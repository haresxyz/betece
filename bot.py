name: Odos One-Time Swap (Optimism)

on:
  workflow_dispatch: {}    # can be run manually from Actions tab
  schedule:
    - cron: "0 6 * * 5"    # automatically runs every Friday at 06:00 UTC

jobs:
  run-swap:
    runs-on: ubuntu-latest
    concurrency:
      group: odos-swap
      cancel-in-progress: false

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run bot.py
        env:
          RPC_URL: ${{ secrets.RPC_URL }}
          PRIVATE_KEY: ${{ secrets.PRIVATE_KEY }}
          WALLET_ADDRESS: ${{ secrets.WALLET_ADDRESS }}
          AMOUNT_IN: ${{ secrets.AMOUNT_IN }}
          TOKEN_IN: ${{ secrets.TOKEN_IN }}
          TOKEN_OUT: ${{ secrets.TOKEN_OUT }}
          SLIPPAGE_PERCENT: ${{ secrets.SLIPPAGE_PERCENT }}
          SEND_TO: ${{ secrets.SEND_TO }}
        run: |
          python bot.py
