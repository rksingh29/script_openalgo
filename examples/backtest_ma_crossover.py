import os
import pandas as pd
from openalgo import api

def run_historical_simulation():
    # -------------------------------------------------------------------------
    # 1. INITIALIZE OPENALGO CLIENT
    # -------------------------------------------------------------------------
    # Replace with your actual API key and Host URL if different
    OPENALGO_API_KEY = os.environ["OPENALGO_API_KEY"]
    OPENALGO_HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")

    print(f"Connecting to OpenAlgo at {OPENALGO_HOST}...")
    client = api(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)

    # -------------------------------------------------------------------------
    # 2. PULL HISTORICAL DATA
    # -------------------------------------------------------------------------
    # Pulls 1-minute intraday bars for Nifty from your local Historify DB
    print("Fetching yesterday's Nifty historical data from local DB...")
    try:
        df = client.history(
            symbol="NIFTY", 
            exchange="NSE", 
            interval="1m", 
            start_date="2026-10-16",  # Adjust dates as needed
            end_date="2026-10-16",
            source="db"               # Uses Historify local DuckDB cache
        )
    except Exception as e:
        print(f"Error fetching data: {e}")
        print("Please ensure OpenAlgo is running and Historify has cached yesterday's data.")
        return

    if df.empty:
        print("No historical data found for the given criteria.")
        return

    # Ensure data is sorted chronologically
    df = df.sort_values(by="datetime").reset_index(drop=True)
    print(f"Successfully loaded {len(df)} historical candles.")

    # -------------------------------------------------------------------------
    # 3. PRE-CALCULATE INDICATORS (Optional)
    # -------------------------------------------------------------------------
    # Example: Simple Moving Averages for strategy logic
    df['fast_ma'] = df['close'].rolling(window=9).mean()
    df['slow_ma'] = df['close'].rolling(window=21).mean()

    # -------------------------------------------------------------------------
    # 4. SIMULATION LOOP (Backtest Engine)
    # -------------------------------------------------------------------------
    print("\n--- Starting Simulation Loop ---")
    
    # Tracking variables
    position = 0          # 0 = Square-off, 1 = Long, -1 = Short
    entry_price = 0.0
    total_pnl = 0.0
    trade_count = 0

    # Step through the historical data chronologically candle by candle
    for index, row in df.iterrows():
        # Skip rows where indicators aren't calculated yet
        if pd.isna(row['fast_ma']) or pd.isna(row['slow_ma']):
            continue

        current_price = row['close']
        timestamp = row['datetime']

        # --- STRATEGY CONDITIONS ---
        # Fast MA crosses above Slow MA -> Buy/Long Signal
        buy_signal = row['fast_ma'] > row['slow_ma']
        # Fast MA crosses below Slow MA -> Sell/Short Signal
        sell_signal = row['fast_ma'] < row['slow_ma']

        # --- EXECUTION LOGIC (Simulating LE, LX, SE, SX) ---
        if position == 0:
            if buy_signal:
                # Simulated LE (Long Entry)
                position = 1
                entry_price = current_price
                trade_count += 1
                print(f"[{timestamp}] [LE] Entered Long at {current_price}")
            elif sell_signal:
                # Simulated SE (Short Entry)
                position = -1
                entry_price = current_price
                trade_count += 1
                print(f"[{timestamp}] [SE] Entered Short at {current_price}")

        elif position == 1:
            if sell_signal:
                # Simulated LX (Long Exit)
                position = 0
                trade_pnl = current_price - entry_price
                total_pnl += trade_pnl
                print(f"[{timestamp}] [LX] Exited Long at {current_price} | PnL: {trade_pnl:.2f}")
                
                # Optional: Immediately flip to Short (SE)
                position = -1
                entry_price = current_price
                trade_count += 1
                print(f"[{timestamp}] [SE] Flipped Short at {current_price}")

        elif position == -1:
            if buy_signal:
                # Simulated SX (Short Exit)
                position = 0
                trade_pnl = entry_price - current_price
                total_pnl += trade_pnl
                print(f"[{timestamp}] [SX] Exited Short at {current_price} | PnL: {trade_pnl:.2f}")
                
                # Optional: Immediately flip to Long (LE)
                position = 1
                entry_price = current_price
                trade_count += 1
                print(f"[{timestamp}] [LE] Flipped Long at {current_price}")

    # -------------------------------------------------------------------------
    # 5. SIMULATION SUMMARY
    # -------------------------------------------------------------------------
    print("\n--- Simulation Summary ---")
    print(f"Total Trades Executed: {trade_count}")
    print(f"Net Strategy Points (PnL): {total_pnl:.2f}")

if __name__ == "__main__":
    run_historical_simulation()
