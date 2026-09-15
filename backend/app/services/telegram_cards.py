from __future__ import annotations

from io import BytesIO
from pathlib import Path
import logging
import math
import textwrap

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from app.models.entities import Position
from app.schemas.dto import TradingRunOut, TradingTickOut
from app.services.telegram_daily import DailyReportSnapshot


logger = logging.getLogger(__name__)

CARD_SIZE = (1200, 800)
BACKGROUND_PATH = Path(__file__).resolve().parents[1] / "assets" / "telegram_trade_card_bg.png"

_REGULAR_FONT_PATHS = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
)
_BOLD_FONT_PATHS = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/segoeuib.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
)

WHITE = "#F6F8FF"
MUTED = "#9AA8C7"
GREEN = "#38E59C"
RED = "#FF627D"
CYAN = "#54D8FF"
VIOLET = "#A98BFF"
PANEL = (5, 12, 31, 205)
PANEL_BORDER = (96, 118, 168, 90)


def safe_render_position_card(
    position: Position,
    *,
    event: str,
    score: int | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    candles: list[list[float]] | None = None,
) -> bytes | None:
    try:
        return render_position_card(
            position,
            event=event,
            score=score,
            exit_price=exit_price,
            exit_reason=exit_reason,
            candles=candles,
        )
    except (OSError, ValueError):
        logger.exception("Failed to render Telegram position card for %s #%s", position.symbol, position.id)
        return None


def safe_render_cycle_card(
    run: TradingRunOut,
    tick: TradingTickOut,
    *,
    paper_trading: bool,
) -> bytes | None:
    try:
        return render_cycle_card(run, tick, paper_trading=paper_trading)
    except (OSError, ValueError):
        logger.exception("Failed to render Telegram cycle card")
        return None


def safe_render_daily_report_card(snapshot: DailyReportSnapshot) -> bytes | None:
    try:
        return render_daily_report_card(snapshot)
    except (OSError, ValueError):
        logger.exception("Failed to render Telegram daily report card")
        return None


def render_position_card(
    position: Position,
    *,
    event: str,
    score: int | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    candles: list[list[float]] | None = None,
) -> bytes:
    context = position.entry_context or {}
    event_key = event.upper()
    current_price = float(exit_price if exit_price is not None else position.current_price)
    entry_price = float(position.entry_price)
    notional = float(context.get("notional") or entry_price * float(position.volume))
    pnl = float(position.pnl or 0.0)
    pnl_percent = pnl / notional * 100 if notional > 0 else 0.0
    move = _effective_percent(position.side, current_price, entry_price)
    risk_reward = float(context.get("risk_reward_ratio") or 0.0)
    accent = _event_accent(event_key, pnl)

    title_map = {
        "OPENED": "НОВАЯ ПОЗИЦИЯ",
        "POSITION": "АКТИВНАЯ ПОЗИЦИЯ",
        "PROTECTION": "ЗАЩИТА ОБНОВЛЕНА",
        "PARTIAL": "ЧАСТЬ ПРИБЫЛИ ЗАФИКСИРОВАНА",
        "CLOSED": "ПОЗИЦИЯ ЗАКРЫТА",
    }
    primary_label = "СИЛА СИГНАЛА" if event_key == "OPENED" and score is not None else "РЕЗУЛЬТАТ"
    primary_value = f"{score} / 100" if primary_label == "СИЛА СИГНАЛА" else f"{pnl:+.2f} USDT"
    primary_hint = (
        "PAPER · ТЕСТОВЫЕ СРЕДСТВА"
        if bool(context.get("paper_exploration"))
        else f"{pnl_percent:+.2f}% · движение {move:+.2f}%"
    )

    metrics = [
        ("ВХОД", _price(entry_price)),
        ("ТЕКУЩАЯ / ВЫХОД", _price(current_price)),
        ("STOP LOSS", _price(float(position.stop))),
        ("TAKE PROFIT", _price(float(position.take))),
    ]
    footer_parts = [
        f"ОБЪЁМ {float(position.volume):.8f}",
        f"СУММА {notional:.2f} USDT",
    ]
    if risk_reward > 0:
        footer_parts.append(f"R/R 1:{risk_reward:.2f}")
    if exit_reason:
        footer_parts.append(str(exit_reason).replace("_", " "))

    image = _base_card(accent)
    draw = ImageDraw.Draw(image, "RGBA")
    _header(
        draw,
        title=title_map.get(event_key, event_key),
        symbol=position.symbol,
        side=position.side,
        accent=accent,
        identifier=f"#{position.id}",
    )
    _primary_block(draw, primary_label, primary_value, primary_hint, accent)
    normalized_candles = _normalize_candles(candles)
    if normalized_candles:
        _candlestick_chart(
            draw,
            normalized_candles,
            entry=entry_price,
            current=current_price,
            stop=float(position.stop),
            take=float(position.take),
        )
        _compact_metrics(draw, metrics, accent)
        _footer(draw, "  ·  ".join(footer_parts), line_y=682, text_y=702, width=82)
    else:
        _metric_grid(draw, metrics, accent)
        _footer(draw, "  ·  ".join(footer_parts))
    return _encode(image)


def render_cycle_card(
    run: TradingRunOut,
    tick: TradingTickOut,
    *,
    paper_trading: bool,
) -> bytes:
    accent = CYAN
    image = _base_card(accent)
    draw = ImageDraw.Draw(image, "RGBA")
    mode = "PAPER" if paper_trading else "LIVE"
    _header(
        draw,
        title="ТОРГОВАЯ СВОДКА",
        symbol=f"ЦИКЛ · {mode}",
        side="",
        accent=accent,
        identifier="CRYBOTHUNTER",
    )
    _primary_block(
        draw,
        "АКТИВНЫХ ПОЗИЦИЙ",
        str(tick.checked),
        f"открыто {run.opened} · закрыто {tick.closed} · пропущено {run.skipped}",
        accent,
    )
    metrics = [
        ("ПРОВЕРЕНО РЫНКОВ", str(run.scanned)),
        ("НОВЫХ ВХОДОВ", str(run.opened)),
        ("ПОЗИЦИЙ ПРОВЕРЕНО", str(tick.checked)),
        ("ОБУЧАЮЩИХ СОБЫТИЙ", str(tick.closed)),
    ]
    _metric_grid(draw, metrics, accent)
    best = sorted(run.decisions, key=lambda item: item.score, reverse=True)[:3]
    summary = " · ".join(f"{item.symbol} {item.signal} {item.score}/100" for item in best)
    _footer(draw, summary or "Сигналов для отображения пока нет")
    return _encode(image)


def render_daily_report_card(snapshot: DailyReportSnapshot) -> bytes:
    accent = GREEN if snapshot.pnl_day >= 0 else RED
    image = _base_card(accent)
    draw = ImageDraw.Draw(image, "RGBA")
    mode = "PAPER" if snapshot.paper_trading else "LIVE"
    _header(
        draw,
        title="ЕЖЕДНЕВНЫЙ ОТЧЁТ",
        symbol=f"ПОРТФЕЛЬ · {mode}",
        side="",
        accent=accent,
        identifier="CRYBOTHUNTER",
    )
    _primary_block(
        draw,
        "PNL ЗА ДЕНЬ",
        f"{snapshot.pnl_day:+.2f} USDT",
        f"открытых {len(snapshot.positions)} · закрыто сегодня {snapshot.closed_today}",
        accent,
    )
    metrics = [
        ("ОТКРЫТЫЙ PNL", f"{snapshot.open_pnl:+.2f} USDT"),
        ("ОБЩИЙ PNL", f"{snapshot.total_pnl:+.2f} USDT"),
        ("ПРИБЫЛЬНЫХ СДЕЛОК", f"{snapshot.win_rate:.2f}%"),
        ("ВОРКЕРЫ", f"{snapshot.healthy_workers} / {snapshot.total_workers}"),
    ]
    _metric_grid(draw, metrics, accent)
    _footer(
        draw,
        (
            f"СДЕЛОК {snapshot.trades_count}  ·  ПРАВИЛ {snapshot.learning_rules}  ·  "
            f"OUTBOX {snapshot.pending_notifications}  ·  {snapshot.generated_at:%d.%m.%Y %H:%M} UTC"
        ),
    )
    return _encode(image)


def _base_card(accent: str) -> Image.Image:
    with Image.open(BACKGROUND_PATH) as source:
        background = source.convert("RGB").resize(CARD_SIZE, Image.Resampling.LANCZOS)
    background = ImageEnhance.Brightness(background).enhance(0.72)
    background = background.filter(ImageFilter.GaussianBlur(radius=0.35)).convert("RGBA")
    overlay = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    draw.rounded_rectangle((45, 40, 1155, 755), radius=42, fill=PANEL, outline=PANEL_BORDER, width=2)
    draw.rounded_rectangle((45, 40, 58, 755), radius=6, fill=accent)
    return Image.alpha_composite(background, overlay)


def _header(
    draw: ImageDraw.ImageDraw,
    *,
    title: str,
    symbol: str,
    side: str,
    accent: str,
    identifier: str,
) -> None:
    draw.text((88, 78), title, font=_font(25, bold=True), fill=accent)
    draw.text((88, 126), symbol, font=_font(52, bold=True), fill=WHITE)
    side_label = "LONG · ПОКУПКА" if side.upper() == "LONG" else "SHORT · ПРОДАЖА" if side else ""
    if side_label:
        draw.text((91, 190), side_label, font=_font(22, bold=True), fill=MUTED)
    identifier_width = draw.textbbox((0, 0), identifier, font=_font(20, bold=True))[2]
    draw.text((1110 - identifier_width, 84), identifier, font=_font(20, bold=True), fill=MUTED)


def _primary_block(
    draw: ImageDraw.ImageDraw,
    label: str,
    value: str,
    hint: str,
    accent: str,
) -> None:
    draw.rounded_rectangle((700, 112, 1110, 260), radius=24, fill=(4, 12, 32, 185), outline=PANEL_BORDER, width=2)
    draw.text((730, 136), label, font=_font(18, bold=True), fill=MUTED)
    draw.text((730, 170), value, font=_font(40, bold=True), fill=accent)
    draw.text((730, 224), hint[:45], font=_font(16), fill=WHITE)


def _metric_grid(draw: ImageDraw.ImageDraw, metrics: list[tuple[str, str]], accent: str) -> None:
    boxes = (
        (88, 310, 575, 445),
        (625, 310, 1110, 445),
        (88, 475, 575, 610),
        (625, 475, 1110, 610),
    )
    for box, (label, value) in zip(boxes, metrics, strict=True):
        draw.rounded_rectangle(box, radius=22, fill=(4, 12, 32, 180), outline=PANEL_BORDER, width=2)
        draw.text((box[0] + 28, box[1] + 25), label, font=_font(17, bold=True), fill=MUTED)
        draw.text((box[0] + 28, box[1] + 62), value, font=_font(31, bold=True), fill=WHITE)
        draw.rounded_rectangle((box[0] + 28, box[3] - 18, box[0] + 104, box[3] - 12), radius=3, fill=accent)


def _compact_metrics(draw: ImageDraw.ImageDraw, metrics: list[tuple[str, str]], accent: str) -> None:
    left, right, gap = 88, 1110, 14
    width = (right - left - gap * 3) // 4
    for index, (label, value) in enumerate(metrics):
        x1 = left + index * (width + gap)
        x2 = x1 + width
        draw.rounded_rectangle((x1, 560, x2, 660), radius=18, fill=(4, 12, 32, 180), outline=PANEL_BORDER, width=2)
        draw.text((x1 + 18, 578), label, font=_font(13, bold=True), fill=MUTED)
        draw.text((x1 + 18, 610), value, font=_font(22, bold=True), fill=WHITE)
        draw.rounded_rectangle((x1 + 18, 646, x1 + 76, 651), radius=2, fill=accent)


def _candlestick_chart(
    draw: ImageDraw.ImageDraw,
    candles: list[tuple[float, float, float, float, float]],
    *,
    entry: float,
    current: float,
    stop: float,
    take: float,
) -> None:
    panel = (88, 285, 1110, 540)
    draw.rounded_rectangle(panel, radius=22, fill=(4, 12, 32, 190), outline=PANEL_BORDER, width=2)
    draw.text((110, 302), f"REAL MARKET · BINANCE · 1H · {len(candles)} CANDLES", font=_font(15, bold=True), fill=MUTED)

    plot_left, plot_top, plot_right, plot_bottom = 110, 334, 900, 516
    reference_lines = (
        ("ENTRY", entry, MUTED),
        ("NOW", current, CYAN),
        ("SL", stop, RED),
        ("TP", take, GREEN),
    )
    low = min([item[3] for item in candles] + [value for _, value, _ in reference_lines])
    high = max([item[2] for item in candles] + [value for _, value, _ in reference_lines])
    spread = max(high - low, max(abs(high), 1.0) * 0.001)
    low -= spread * 0.06
    high += spread * 0.06

    def price_y(price: float) -> int:
        ratio = (high - price) / (high - low)
        return int(plot_top + ratio * (plot_bottom - plot_top))

    for index in range(5):
        y = int(plot_top + index * (plot_bottom - plot_top) / 4)
        draw.line((plot_left, y, plot_right, y), fill=(96, 118, 168, 35), width=1)

    step = (plot_right - plot_left) / max(len(candles), 1)
    body_width = max(min(int(step * 0.58), 10), 3)
    for index, (_, open_price, high_price, low_price, close_price) in enumerate(candles):
        x = int(plot_left + step * (index + 0.5))
        color = GREEN if close_price >= open_price else RED
        draw.line((x, price_y(high_price), x, price_y(low_price)), fill=color, width=2)
        open_y = price_y(open_price)
        close_y = price_y(close_price)
        body_top, body_bottom = sorted((open_y, close_y))
        if body_bottom - body_top < 2:
            body_bottom = body_top + 2
        draw.rounded_rectangle(
            (x - body_width // 2, body_top, x + body_width // 2, body_bottom),
            radius=1,
            fill=color,
        )

    label_positions = _spread_chart_labels([(price_y(value), label, value, color) for label, value, color in reference_lines])
    for line_y, label_y, label, value, color in label_positions:
        draw.line((plot_left, line_y, 1088, line_y), fill=color, width=2)
        draw.rounded_rectangle((914, label_y - 2, 1089, label_y + 18), radius=7, fill=(4, 12, 32, 235))
        draw.text((924, label_y), f"{label}  {_price(value)}", font=_font(13, bold=True), fill=color)


def _spread_chart_labels(
    labels: list[tuple[int, str, float, str]],
) -> list[tuple[int, int, str, float, str]]:
    ordered = sorted(labels, key=lambda item: item[0])
    positions: list[list[object]] = []
    previous = 330
    for line_y, label, value, color in ordered:
        label_y = max(line_y - 8, previous + 21)
        positions.append([line_y, label_y, label, value, color])
        previous = label_y
    overflow = max(int(positions[-1][1]) - 511, 0) if positions else 0
    if overflow:
        for item in positions:
            item[1] = int(item[1]) - overflow
    return [
        (int(item[0]), int(item[1]), str(item[2]), float(item[3]), str(item[4]))
        for item in positions
    ]


def _normalize_candles(candles: list[list[float]] | None) -> list[tuple[float, float, float, float, float]]:
    normalized: list[tuple[float, float, float, float, float]] = []
    for row in candles or []:
        if len(row) < 5:
            continue
        try:
            timestamp, open_price, high_price, low_price, close_price = (float(value) for value in row[:5])
        except (TypeError, ValueError):
            continue
        values = (timestamp, open_price, high_price, low_price, close_price)
        if not all(math.isfinite(value) for value in values):
            continue
        if min(open_price, high_price, low_price, close_price) <= 0 or high_price < low_price:
            continue
        normalized.append(values)
    return normalized[-48:] if len(normalized) >= 8 else []


def _footer(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    line_y: int = 653,
    text_y: int = 678,
    width: int = 76,
) -> None:
    wrapped = textwrap.wrap(text, width=width)[:2]
    draw.line((88, line_y, 1110, line_y), fill=PANEL_BORDER, width=2)
    for index, line in enumerate(wrapped):
        draw.text((88, text_y + index * 27), line, font=_font(18, bold=index == 0), fill=MUTED if index else WHITE)


def _event_accent(event: str, pnl: float) -> str:
    if event == "CLOSED":
        return GREEN if pnl >= 0 else RED
    if event == "PROTECTION":
        return VIOLET
    if event == "PARTIAL":
        return GREEN
    return CYAN


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _BOLD_FONT_PATHS if bold else _REGULAR_FONT_PATHS:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def _encode(image: Image.Image) -> bytes:
    target = BytesIO()
    image.convert("RGB").save(target, format="JPEG", quality=88, optimize=True, progressive=True)
    return target.getvalue()


def _effective_percent(side: str, current: float, entry: float) -> float:
    if entry == 0:
        return 0.0
    movement = (current / entry - 1) * 100
    return movement if side.upper() == "LONG" else -movement


def _price(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.2f}".replace(",", " ")
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}"
