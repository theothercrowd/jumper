"""
batch_jumper_bridge.py
Python 3.12 • web3.py ≥7 • requests
pip install web3 requests
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests
from web3 import Web3
from web3.types import TxParams

# ───────────────────────────── CONFIG ───────────────────────────── #

CONFIG: Dict = {
    # random delay between wallets (seconds)
    "min_sleep_sec": 1,
    "max_sleep_sec": 6,

    # bridge this % of wallet's token balance
    "bridge_percent": 99.7,          # e.g. 50 → half the balance

    # slippage for LI.FI quote (percent)
    "slippage": 0.1,

    # map chain_id → RPC URL
    "chains": {
        1:   "https://ethereum-rpc.publicnode.com",   # Ethereum
        137: "https://polygon-rpc.com",                        # Polygon
        56:  "https://bsc-dataseed.binance.org",               # BSC
        1285: "https://rpc.api.moonriver.moonbeam.network",    # Moonriver
        100: "https://1rpc.io/gnosis",                         # Gnosis XDAI
        42161: "https://arb1.arbitrum.io/rpc",                 # Arbitrum
        10: "https://mainnet.optimism.io",                     # Optimism
        534352: "https://scroll-mainnet.public.blastapi.io",   # Scroll
        167000: "https://rpc.taiko.xyz",                       # Taiko
        8453: "https://mainnet.base.org",                      # Base
    },

    # choose source and destination chains
    "from_chain": 100,   # 
    "to_chain": 8453,      # 

    # token addresses (ERC-20) or "" / None for native coin
    "from_token": "",  # Native
    "to_token":   "",  # Native
}

# ──────────────────────────── CONSTANTS ─────────────────────────── #

WALLETS_FILE   = "wallets.txt"
PROXIES_FILE   = "proxy.txt"
BASE_URL       = "https://li.quest/v1"
QUOTE_URL      = f"{BASE_URL}/quote"
CHAINS_URL     = f"{BASE_URL}/chains"
TOKENS_URL     = f"{BASE_URL}/tokens"
NATIVE_TOKEN   = "0x0000000000000000000000000000000000000000"  # LI.FI native token placeholder

# minimal ERC-20 ABI: allowance, balanceOf, approve
ERC20_ABI = json.loads("""
[
  {"constant":true,"inputs":[{"name":"","type":"address"},{"name":"","type":"address"}],
   "name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":true,"inputs":[{"name":"_owner","type":"address"}],
   "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
   "name":"approve","outputs":[{"name":"success","type":"bool"}],"type":"function"}
]
""")

# ───────────────────────────── MODELS ───────────────────────────── #


@dataclass(slots=True)
class JumperQuote:
    from_chain: int
    to_chain: int
    from_token: str
    to_token: str
    from_addr: str
    to_addr: str
    from_amount: int
    slippage: float
    quote_id: Optional[str] = None
    tx_data: Optional[Dict] = None
    to_amount: Optional[int] = None

    def fetch(self) -> None:
        # Prepare parameters for quote endpoint only
        params = {
            "fromChain": str(self.from_chain),
            "toChain": str(self.to_chain), 
            "fromToken": self.from_token or NATIVE_TOKEN,
            "toToken": self.to_token or NATIVE_TOKEN,
            "fromAddress": self.from_addr,
            "fromAmount": str(self.from_amount),
            "slippage": str(self.slippage / 100),  # Convert to decimal string
        }
        
        # Only add toAddress if it's different from fromAddress
        if self.to_addr != self.from_addr:
            params["toAddress"] = self.to_addr
        
        # Get random proxy for this request
        proxies = get_random_proxy()
        proxy_info = f" via proxy {list(proxies.values())[0].split('@')[1] if proxies else 'none'}" if proxies else " (no proxy)"
        print(f"  ↪ Requesting quote{proxy_info}")
        
        try:
            headers = {'accept': 'application/json'}
            r = requests.get(QUOTE_URL, params=params, headers=headers, timeout=30, proxies=proxies)
            
            if r.status_code == 404:
                raise requests.exceptions.HTTPError(f"404 - No bridge available for {self.from_chain} -> {self.to_chain}")
            
            r.raise_for_status()
            data = r.json()

            self.quote_id = data.get("id")
            self.tx_data = (data.get("transactionRequest") or 
                           data.get("action", {}).get("transactionRequest"))
            
            if not self.tx_data:
                raise RuntimeError("No transaction data in quote response")
            
            # Try different response structures for output amount
            out_str = (data.get("estimate", {}).get("toAmount") or 
                      data.get("toAmount") or
                      data.get("action", {}).get("toAmount"))
            
            if out_str is None:
                raise RuntimeError("LI.FI response missing toAmount")
            self.to_amount = int(out_str)
                
        except Exception as e:
            raise RuntimeError(f"Quote endpoint failed. Route may not be supported. Error: {e}")


class Jumper:
    """Sync helper that sends approval and swap txs via web3.py"""

    def __init__(self, w3: Web3, account: str, priv_key: str) -> None:
        self.w3 = w3
        self.account = Web3.to_checksum_address(account)
        self.key = priv_key

    # ─────────── low-level helpers ─────────── #

    def _erc20(self, addr: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(addr), abi=ERC20_ABI
        )

    def _allowance_ok(self, token: str, spender: str, target: int) -> bool:
        return (
            self._erc20(token)
            .functions.allowance(self.account, Web3.to_checksum_address(spender))
            .call()
            >= target
        )

    def _approve(self, token: str, spender: str, amount: int) -> None:
        nonce = self.w3.eth.get_transaction_count(self.account)
        
        # Get current gas price
        gas_price = self.w3.eth.gas_price
        
        tx = (
            self._erc20(token)
            .functions.approve(spender, amount)
            .build_transaction(
                {
                    "from": self.account,
                    "nonce": nonce,
                    "gas": 100_000,  # Increased gas limit
                    "gasPrice": gas_price,
                    "chainId": self.w3.eth.chain_id,
                }
            )
        )
        signed = self.w3.eth.account.sign_transaction(tx, self.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        self._await(tx_hash, "approval")

    def _await(self, tx_hash, label="tx") -> None:
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status != 1:
            raise RuntimeError(f"{label} failed: {tx_hash.hex()}")

    # ───────────── public API ───────────── #

    def swap(self, quote: JumperQuote) -> str:
        if not quote.tx_data:
            raise ValueError("Quote not fetched")

        tx: TxParams = dict(quote.tx_data)  # shallow copy
        
        # Handle gas field conversion - LI.FI might use 'gasLimit' instead of 'gas'
        for f in ("gasLimit", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "value", "gas"):
            if f in tx:
                tx[f] = int(str(tx[f]), 0)
        
    def swap(self, quote: JumperQuote) -> str:
        if not quote.tx_data:
            raise ValueError("Quote not fetched")

        tx: TxParams = dict(quote.tx_data)  # shallow copy
        
        # Handle gas field conversion - LI.FI might use 'gasLimit' instead of 'gas'
        for f in ("gasLimit", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "value", "gas"):
            if f in tx:
                tx[f] = int(str(tx[f]), 0)
        
        # Ensure gas field exists (convert gasLimit to gas if needed)
        if "gas" not in tx and "gasLimit" in tx:
            tx["gas"] = tx.pop("gasLimit")
        elif "gas" not in tx:
            # Estimate gas if not provided
            try:
                estimated_gas = self.w3.eth.estimate_gas({
                    'to': tx.get('to'),
                    'data': tx.get('data', '0x'),
                    'value': tx.get('value', 0),
                    'from': self.account
                })
                tx["gas"] = int(estimated_gas * 1.2)  # Add 20% buffer
                print(f"  ↪ Estimated gas: {tx['gas']}")
            except Exception as e:
                print(f"  ↪ Gas estimation failed ({e}), using default: 300000")
                tx["gas"] = 300000

        # Ensure EIP-155 compliance by setting chainId
        tx["chainId"] = self.w3.eth.chain_id
        tx["from"] = self.account
        tx["nonce"] = self.w3.eth.get_transaction_count(self.account)
        
        print(f"  ↪ Transaction details: gas={tx.get('gas')}, chainId={tx.get('chainId')}, value={tx.get('value', 0)}")

        # approve if ERC-20 input and insufficient allowance
        if quote.from_token and quote.from_token != NATIVE_TOKEN:
            spender = tx["to"]
            if not self._allowance_ok(quote.from_token, spender, quote.from_amount):
                self._approve(quote.from_token, spender, quote.from_amount)

        signed = self.w3.eth.account.sign_transaction(tx, self.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        self._await(tx_hash, "swap")
        return tx_hash.hex()


# ──────────────────────────── UTILITIES ─────────────────────────── #

def load_private_keys(path: str = WALLETS_FILE) -> List[str]:
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


def load_proxies(path: str = PROXIES_FILE) -> List[str]:
    """Load proxies from file in format: http://login:pass@ip:port"""
    try:
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        print(f"  ↪ Warning: {path} not found, running without proxies")
        return []


def get_random_proxy() -> Optional[Dict[str, str]]:
    """Get a random proxy for requests"""
    proxies_list = load_proxies()
    if not proxies_list:
        return None
    
    proxy_url = random.choice(proxies_list)
    return {
        'http': proxy_url,
        'https': proxy_url
    }


def check_chain_support(chain_id: int) -> bool:
    """Check if a chain is supported by LI.FI"""
    try:
        proxies = get_random_proxy()
        r = requests.get(CHAINS_URL, timeout=10, proxies=proxies)
        r.raise_for_status()
        chains = r.json()
        
        supported_chains = [chain.get('id') for chain in chains.get('chains', [])]
        return chain_id in supported_chains
    except Exception as e:
        print(f"  ↪ Warning: Could not verify chain support ({e})")
        return True  # Assume supported if check fails


def random_sleep(min_s: int, max_s: int) -> None:
    delay = random.uniform(min_s, max_s)
    print(f"  ↪ sleeping {delay:.2f}s")
    time.sleep(delay)


# ───────────────────────────── MAIN ─────────────────────────────── #

def main() -> None:
    keys = load_private_keys()
    if not keys:
        raise SystemExit(f"No private keys found in {WALLETS_FILE}")

    fc = CONFIG["from_chain"]
    tc = CONFIG["to_chain"]
    from_rpc = CONFIG["chains"][fc]
    from_token = CONFIG["from_token"] or ""
    to_token = CONFIG["to_token"] or ""
    bridge_pct = CONFIG["bridge_percent"]
    slippage = CONFIG["slippage"]

    # Check chain support
    print(f"Checking chain support...")
    if not check_chain_support(fc):
        print(f"  ↪ Warning: From chain {fc} may not be supported")
    if not check_chain_support(tc):
        print(f"  ↪ Warning: To chain {tc} may not be supported")

    print(
        f"Launching batch bridge: {len(keys)} wallets | "
        f"{bridge_pct}% of {from_token or 'native'} → {to_token or 'native'} | "
        f"{fc}→{tc}"
    )

    for idx, pk in enumerate(keys, 1):
        w3 = Web3(Web3.HTTPProvider(from_rpc))
        acct = w3.eth.account.from_key(pk).address
        print(f"\n[{idx}/{len(keys)}] {acct}")

        # fetch balance of input asset
        if from_token:
            bal = (
                w3.eth.contract(address=Web3.to_checksum_address(from_token), abi=ERC20_ABI)
                .functions.balanceOf(acct)
                .call()
            )
        else:
            bal = w3.eth.get_balance(acct)

        amount = int(bal * bridge_pct / 100)
        if amount == 0:
            print("  ↪ balance 0 – skipping")
            continue

        print(f"  ↪ Balance: {bal}, bridging: {amount}")

        try:
            quote = JumperQuote(
                from_chain=fc,
                to_chain=tc,
                from_token=from_token,
                to_token=to_token,
                from_addr=acct,
                to_addr=acct,
                from_amount=amount,
                slippage=slippage,
            )
            quote.fetch()
            print(f"  ↪ quote ok – will receive ≈{quote.to_amount}")

            bridge = Jumper(w3, acct, pk)
            tx_hash = bridge.swap(quote)
            print(f"  ↪ tx sent: {tx_hash}")
        except Exception as e:
            print(f"  ↪ ERROR: {e}")

        random_sleep(CONFIG["min_sleep_sec"], CONFIG["max_sleep_sec"])


if __name__ == "__main__":
    main()
