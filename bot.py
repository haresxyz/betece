import os, math, json, requests
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware.proof_of_authority import ExtraDataToPOAMiddleware

CHAIN_ID = 10  # Optimism
ODOS_API = "https://api.odos.xyz"

ERC20_ABI = json.loads("""[
  {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
  {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
   "name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"value","type":"uint256"}],
   "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
  {"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
   "outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"recipient","type":"address"},{"name":"amount","type":"uint256"}],
   "name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}
]""")

def to_wei(amount: Decimal, decimals: int) -> int:
    q = Decimal(10) ** decimals
    return int((amount * q).to_integral_value(rounding=ROUND_DOWN))

def from_wei(amount: int, decimals: int) -> Decimal:
    return Decimal(amount) / (Decimal(10) ** decimals)

def odos_get_router(chain_id: int) -> str:
    url = f"{ODOS_API}/info/router/v2/{chain_id}"
    r = requests.get(url, timeout=20)
    if r.status_code != 200:
        raise SystemExit(f"Failed to fetch Odos router: {r.status_code} {r.text}")
    return Web3.to_checksum_address(r.json()["address"])

def odos_quote(chain_id: int, wallet: str, token_in: str, amount_in_wei: int,
               token_out: str, slippage_percent: Decimal, include_user=True) -> requests.Response:
    body = {
        "chainId": chain_id,
        "inputTokens": [{"tokenAddress": token_in, "amount": str(amount_in_wei)}],
        "outputTokens": [{"tokenAddress": token_out, "proportion": 1}],
        "slippageLimitPercent": float(slippage_percent),
        "compact": True
    }
    if include_user:
        body["userAddr"] = wallet
    return requests.post(f"{ODOS_API}/sor/quote/v2", json=body, timeout=30)

def odos_assemble(path_id: str, wallet: str, simulate: bool = False) -> dict:
    body = {"userAddr": wallet, "pathId": path_id, "simulate": simulate}
    r = requests.post(f"{ODOS_API}/sor/assemble", json=body, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"Assemble failed: {r.status_code} {r.text}")
    return r.json()

def sign_and_send(w3, tx, private_key):
    signed = w3.eth.account.sign_transaction(tx, private_key)
    return w3.eth.send_raw_transaction(signed.raw_transaction)

def main():
    load_dotenv()

    RPC_URL       = os.getenv("RPC_URL")
    PRIVATE_KEY   = os.getenv("PRIVATE_KEY")
    WALLET        = Web3.to_checksum_address(os.getenv("WALLET_ADDRESS"))
    AMOUNT_IN     = Decimal(os.getenv("AMOUNT_IN", "0.1"))
    TOKEN_IN_ADDR = Web3.to_checksum_address(os.getenv("TOKEN_IN"))
    TOKEN_OUT_ADDR= Web3.to_checksum_address(os.getenv("TOKEN_OUT"))
    SLIPPAGE_PCT  = Decimal(os.getenv("SLIPPAGE_PERCENT", "0.5"))
    SEND_TO_RAW   = (os.getenv("SEND_TO") or "").strip()
    SEND_TO       = Web3.to_checksum_address(SEND_TO_RAW) if SEND_TO_RAW else None

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        raise SystemExit("Failed to connect to RPC.")

    acct = w3.eth.account.from_key(PRIVATE_KEY)
    if acct.address.lower() != WALLET.lower():
        raise SystemExit("PRIVATE_KEY does not match WALLET_ADDRESS.")

    token_in  = w3.eth.contract(address=TOKEN_IN_ADDR,  abi=ERC20_ABI)
    token_out = w3.eth.contract(address=TOKEN_OUT_ADDR, abi=ERC20_ABI)
    dec_in    = token_in.functions.decimals().call()
    dec_out   = token_out.functions.decimals().call()

    # Check token balance
    bal_in = token_in.functions.balanceOf(WALLET).call()
    if from_wei(bal_in, dec_in) < AMOUNT_IN:
        raise SystemExit(f"Insufficient token balance. Balance: {from_wei(bal_in, dec_in)}")

    amount_in_wei = to_wei(AMOUNT_IN, dec_in)
    router = odos_get_router(CHAIN_ID)

    # Request a quote (try with userAddr, then fallback without)
    r = odos_quote(CHAIN_ID, WALLET, TOKEN_IN_ADDR, amount_in_wei, TOKEN_OUT_ADDR, SLIPPAGE_PCT, include_user=True)
    if r.status_code != 200:
        r2 = odos_quote(CHAIN_ID, WALLET, TOKEN_IN_ADDR, amount_in_wei, TOKEN_OUT_ADDR, SLIPPAGE_PCT, include_user=False)
        if r2.status_code != 200:
            raise SystemExit(f"Odos quote failed: {r.status_code} {r.text} / {r2.status_code} {r2.text}")
        quote = r2.json()
        print("Info: Odos quote succeeded without userAddr (fallback).")
    else:
        quote = r.json()

    path_id = quote["pathId"]
    est_out = quote.get("outputTokens", [{}])[0].get("amount")

    # Approve router if needed (unlimited)
    allowance = token_in.functions.allowance(WALLET, router).call()
    if allowance < amount_in_wei:
        nonce = w3.eth.get_transaction_count(WALLET)
        approve_tx = token_in.functions.approve(router, int(2**255)).build_transaction({
            "chainId": CHAIN_ID,
            "from": WALLET,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price
        })
        try:
            approve_tx["gas"] = math.ceil(w3.eth.estimate_gas(approve_tx) * 1.2)
        except Exception:
            approve_tx["gas"] = 120000
        txh = sign_and_send(w3, approve_tx, PRIVATE_KEY)
        rcpt = w3.eth.wait_for_transaction_receipt(txh)
        if rcpt.status != 1:
            raise SystemExit("Approval failed.")
        print(f"Approval successful: {txh.hex()}")

    # Assemble final transaction
    assembled = odos_assemble(path_id, WALLET, simulate=False)
    call = assembled["transaction"]
    to_addr = Web3.to_checksum_address(call["to"])
    data    = call["data"]
    value   = int(call.get("value", "0"), 16) if isinstance(call.get("value"), str) and call["value"].startswith("0x") else int(call.get("value", 0))

    nonce = w3.eth.get_transaction_count(WALLET)
    swap_tx = {
        "chainId": CHAIN_ID,
        "from": WALLET,
        "to": to_addr,
        "data": data,
        "value": value,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price
    }
    try:
        swap_tx["gas"] = math.ceil(w3.eth.estimate_gas(swap_tx) * 1.2)
    except Exception:
        swap_tx["gas"] = 500000

    txh_swap = sign_and_send(w3, swap_tx, PRIVATE_KEY)
    rcpt_swap = w3.eth.wait_for_transaction_receipt(txh_swap)
    if rcpt_swap.status != 1:
        raise SystemExit("Swap failed.")

    if est_out:
        try:
            est_out_dec = from_wei(int(est_out), dec_out)
            print(f"Swap successful: {txh_swap.hex()} | Estimated WBTC: ~{est_out_dec}")
        except Exception:
            print(f"Swap successful: {txh_swap.hex()}")
    else:
        print(f"Swap successful: {txh_swap.hex()}")

    # Optional: send output token to another address
    if SEND_TO:
        bal_out = token_out.functions.balanceOf(WALLET).call()
        if bal_out > 0:
            nonce = w3.eth.get_transaction_count(WALLET)
            tx2 = token_out.functions.transfer(SEND_TO, bal_out).build_transaction({
                "chainId": CHAIN_ID,
                "from": WALLET,
                "nonce": nonce,
                "gasPrice": w3.eth.gas_price
            })
            try:
                tx2["gas"] = math.ceil(w3.eth.estimate_gas(tx2) * 1.2)
            except Exception:
                tx2["gas"] = 120000
            txh2 = sign_and_send(w3, tx2, PRIVATE_KEY)
            rcpt2 = w3.eth.wait_for_transaction_receipt(txh2)
            if rcpt2.status != 1:
                raise SystemExit("WBTC transfer failed.")
            print(f"WBTC transfer successful: {txh2.hex()}")

    print("One-time Odos swap completed.")

if __name__ == "__main__":
    main()
