# Indian Options Market Mechanics & Arbitrage System Rules

Every model response, execution engine module, and quantitative evaluation MUST STRICTLY obey the following official Indian Options Market Mechanics rules (NSE & BSE 2026 Rules):

## 1. Market Hours & Session Cutoffs
- **Pre-Open Session**: 9:00 AM – 9:15 AM IST
- **Continuous Trading Session**: 9:15 AM – 3:15 PM IST (F&O Eligible Stocks & Indices)
- **Closing Auction Session (CAS)**: 3:15 PM – 3:35 PM IST (Effective August 3, 2026: F&O stocks & index contracts halt continuous trading at 3:15 PM and close via CAS; final settlement price derived from CAS equilibrium price).
- **Post-Close Session**: After 3:35 PM IST.
- **Time Zone**: IST (GMT+5:30, Asia/Kolkata). No Daylight Saving Time.

## 2. Expiry Schedule
- **Nifty 50 Weekly Options (NSE)**: Expire **every Tuesday** (Effective since September 1, 2025 per SEBI circular).
- **Nifty 50 Monthly Options (NSE)**: Expire on the **last Tuesday** of the month.
- **Sensex Weekly Options (BSE)**: Expire **every Thursday**.
- **Sensex Monthly Options (BSE)**: Expire on the **last Thursday** of the month.
- **Exchange Holiday Shift Rule**: If Tuesday (Nifty) or Thursday (Sensex) is an exchange holiday, the expiry shifts automatically to the **previous working day**.

## 3. Cash Settlement & Cost Model
- Index Options (Nifty 50, Sensex) are **cash-settled** automatically at CAS equilibrium price. No delivery required.
- Put-Call Parity formula: $\text{Call} - \text{Put} \approx \text{Spot} - \text{Strike} \times e^{-r t}$, where $t = \text{days\_to\_expiry} / 365.0$.
- All trades MUST enforce **Net ROI $\ge 2.50\%$** after deducting STT (0.125% sell / 0.02% fut), NSE turnover charges, SEBI fees, GST (18%), Stamp Duty, and slippage.
