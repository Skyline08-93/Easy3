# triangle_bybit_async_bot.py

import ccxt.async_support as ccxt
import asyncio
import os
import hashlib
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application
from datetime import datetime

# === Telegram настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

# === Основные настройки ===
commission_rate = 0.001  # 0.1%
min_profit = 0.1  # %
max_profit = 3.0  # %
start_coins = ['USDT', 'BTC', 'ETH']
target_volume_usdt = 100  # Целевая ликвидность
sent_hashes = set()
debug_mode = True  # Включает дебаг-вывод в терминал

log_file = "triangle_log.csv"

exchange = ccxt.bybit()


async def load_symbols():
    markets = await exchange.load_markets()
    return list(markets.keys())


async def find_triangles(symbols):
    triangles = []
    for base in start_coins:
        for sym1 in symbols:
            if not sym1.endswith('/' + base): continue
            mid1 = sym1.split('/')[0]
            for sym2 in symbols:
                if not sym2.startswith(mid1 + '/'): continue
                mid2 = sym2.split('/')[1]
                third = f"{mid2}/{base}"
                if third in symbols or f"{base}/{mid2}" in symbols:
                    triangles.append((base, mid1, mid2))
    return triangles


async def get_avg_price(orderbook_side, target_usdt):
    total_base = 0
    total_usd = 0
    max_liquidity = 0
    for price, volume in orderbook_side:
        price = float(price)
        volume = float(volume)
        usd = price * volume
        max_liquidity += usd
        if total_usd + usd >= target_usdt:
            remain_usd = target_usdt - total_usd
            total_base += remain_usd / price
            total_usd += remain_usd
            break
        else:
            total_base += volume
            total_usd += usd
    if total_usd < target_usdt:
        return None, 0, max_liquidity
    avg_price = total_usd / total_base
    return avg_price, total_usd, max_liquidity


async def get_execution_price(symbol, side, target_usdt):
    try:
        orderbook = await exchange.fetch_order_book(symbol)
        if side == "buy":
            return await get_avg_price(orderbook['asks'], target_usdt)
        else:
            return await get_avg_price(orderbook['bids'], target_usdt)
    except Exception as e:
        if debug_mode:
            print(f"[Ошибка стакана {symbol}]: {e}")
        return None, 0, 0


def format_line(index, pair, price, side, volume_usd, color, liquidity):
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🟥"}.get(color, "")
    return f"{emoji} {index}. {pair} - {price:.6f} ({side}), исполнено ${volume_usd:.2f}, доступно ${liquidity:.2f}"


async def send_telegram_message(text):
    try:
        await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        if debug_mode:
            print(f"[Ошибка Telegram]: {e}")


def log_route(base, mid1, mid2, profit, volume):
    with open(log_file, "a") as f:
        f.write(f"{datetime.utcnow()},{base}->{mid1}->{mid2}->{base},{profit:.4f},{volume}\n")


async def check_triangle(base, mid1, mid2, symbols):
    try:
        s1 = f"{mid1}/{base}" if f"{mid1}/{base}" in symbols else f"{base}/{mid1}"
        s2 = f"{mid2}/{mid1}" if f"{mid2}/{mid1}" in symbols else f"{mid1}/{mid2}"
        s3 = f"{mid2}/{base}" if f"{mid2}/{base}" in symbols else f"{base}/{mid2}"

        price1, vol1, liq1 = await get_execution_price(s1, "buy" if f"{mid1}/{base}" in symbols else "sell", target_volume_usdt)
        if not price1: return
        step1 = (1 / price1 if f"{mid1}/{base}" in symbols else price1) * (1 - commission_rate)
        side1 = "ASK" if f"{mid1}/{base}" in symbols else "BID"

        price2, vol2, liq2 = await get_execution_price(s2, "buy" if f"{mid2}/{mid1}" in symbols else "sell", target_volume_usdt)
        if not price2: return
        step2 = (1 / price2 if f"{mid2}/{mid1}" in symbols else price2) * (1 - commission_rate)
        side2 = "ASK" if f"{mid2}/{mid1}" in symbols else "BID"

        price3, vol3, liq3 = await get_execution_price(s3, "sell" if f"{mid2}/{base}" in symbols else "buy", target_volume_usdt)
        if not price3: return
        step3 = (price3 if f"{mid2}/{base}" in symbols else 1 / price3) * (1 - commission_rate)
        side3 = "BID" if f"{mid2}/{base}" in symbols else "ASK"

        result = step1 * step2 * step3
        profit_percent = (result - 1) * 100
        if not (min_profit <= profit_percent <= max_profit): return

        route_hash = hashlib.md5(f"{base}->{mid1}->{mid2}->{base}".encode()).hexdigest()
        if route_hash in sent_hashes:
            return
        sent_hashes.add(route_hash)

        min_liquidity = round(min(liq1, liq2, liq3), 2)
        pure_profit_usdt = round((result - 1) * target_volume_usdt, 2)

        message = "\n".join([
            format_line(1, s1, price1, side1, vol1, "green", liq1),
            format_line(2, s2, price2, side2, vol2, "yellow", liq2),
            format_line(3, s3, price3, side3, vol3, "red", liq3),
            "",
            f"💰 Чистая прибыль: {pure_profit_usdt:.2f} USDT",
            f"📈 Спред: {profit_percent:.2f}%",
            f"💧 Мин. ликвидность на шаге: ${min_liquidity}"
        ])

        if debug_mode:
            print(f"📤 Telegram:\n{message}\n")

        await send_telegram_message(message)
        log_route(base, mid1, mid2, profit_percent, min_liquidity)

    except Exception as e:
        if debug_mode:
            print(f"[Ошибка маршрута]: {e}")


async def main():
    try:
        symbols = await load_symbols()
        triangles = await find_triangles(symbols)
        if debug_mode:
            print(f"🔁 Найдено маршрутов: {len(triangles)}")

        await telegram_app.initialize()
        await telegram_app.start()

        while True:
            tasks = [check_triangle(base, mid1, mid2, symbols) for base, mid1, mid2 in triangles]
            await asyncio.gather(*tasks)
            await asyncio.sleep(10)
    except KeyboardInterrupt:
        print("Остановка...")
    finally:
        await exchange.close()
        await telegram_app.stop()
        await telegram_app.shutdown()


if __name__ == '__main__':
    asyncio.run(main())