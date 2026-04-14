"""
Утилита: получить данные инструментов из T-Invest API по списку тикеров.
Выводит готовые блоки для instruments.yaml.

Запуск:
    python fetch_instruments.py
"""
import os
from dotenv import load_dotenv
from tinkoff.invest import Client, InstrumentIdType
from tinkoff.invest.utils import quotation_to_decimal

load_dotenv()
TOKEN = os.environ["TINKOFF_TOKEN"]

TICKERS = [
    "NVTK",   # Новатэк
    "ROSN",   # Роснефть
    "TATN",   # Татнефть
    "YNDX",   # Яндекс
    "PLZL",   # Полюс
    "CHMF",   # Северсталь
    "NLMK",   # НЛМК
    "MAGN",   # ММК
    "ALRS",   # Алроса
    "MTSS",   # МТС
    # Новые тикеры — уточнить instrument_id в instruments.yaml после запуска
    "SIBN",   # Газпром нефть
    "AFLT",   # Аэрофлот
    "MGNT",   # Магнит
    "MOEX",   # Московская биржа
    "PHOR",   # ФосАгро
]

with Client(TOKEN) as client:
    for ticker in TICKERS:
        try:
            resp = client.instruments.share_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                class_code="TQBR",
                id=ticker,
            )
            s = resp.instrument
            tick_size = float(quotation_to_decimal(s.min_price_increment))
            price_approx = tick_size * 1000  # очень грубо, просто для справки
            print(f"{ticker}:")
            print(f"  figi: {s.figi}")
            print(f"  instrument_id: {s.uid}")
            print(f"  lot_size: {s.lot}")
            print(f"  tick_size: {tick_size}")
            print(f"  # name: {s.name}")
            print()
        except Exception as e:
            print(f"# {ticker}: ошибка — {e}")
            print()
