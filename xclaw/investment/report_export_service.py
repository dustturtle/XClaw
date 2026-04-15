"""Generate PDF and image assets for investment reports."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from xclaw.config import Settings


class ReportExportService:
    """Export stored markdown reports to PDF and PNG assets."""

    def __init__(self, *, db: Any, settings: Settings | None = None) -> None:
        self._db = db
        self._settings = settings or Settings()
        try:
            pdfmetrics.getFont("STSong-Light")
        except KeyError:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        self._font_path = self._pick_font_path()
        self._plot_font = FontProperties(fname=str(self._font_path)) if self._font_path else None

    async def generate_assets(self, report_id: int) -> dict[str, Any]:
        report = await self._db.get_investment_report(report_id)
        if report is None:
            raise ValueError(f"unknown report_id={report_id}")

        report_dir = self._settings.report_exports_path / str(report["chat_id"]) / str(report_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        await self._db.clear_report_exports(report_id)

        summary_png = report_dir / "summary_card.png"
        chart_png = report_dir / "strategy_counts.png"
        pdf_path = report_dir / "report.pdf"

        parsed = self._parse_report(report["content_markdown"])
        self._render_summary_card(report, parsed, summary_png)
        self._render_strategy_chart(parsed, chart_png)
        self._render_pdf(report, pdf_path)

        summary_export = await self._record_export(report_id, "summary_png", "image/png", summary_png)
        chart_export = await self._record_export(report_id, "chart_png", "image/png", chart_png)
        pdf_export = await self._record_export(report_id, "pdf", "application/pdf", pdf_path)

        return {
            "pdf": pdf_export,
            "images": [summary_export, chart_export],
        }

    async def _record_export(
        self,
        report_id: int,
        asset_type: str,
        mime_type: str,
        file_path: Path,
    ) -> dict[str, Any]:
        export_id = await self._db.add_report_export(
            report_id=report_id,
            asset_type=asset_type,
            mime_type=mime_type,
            file_path=str(file_path),
            status="ready",
        )
        rows = await self._db.list_report_exports(report_id)
        row = next(item for item in rows if item["id"] == export_id)
        return row

    def _render_summary_card(self, report: dict[str, Any], parsed: dict[str, Any], output_path: Path) -> None:
        image = Image.new("RGB", (1200, 1600), color=(249, 245, 238))
        draw = ImageDraw.Draw(image)
        title_font = self._load_pil_font(36)
        body_font = self._load_pil_font(24)
        y = 40
        self._draw_block(draw, 40, y, report["title"], fill=(48, 36, 24), font=title_font)
        y += 80
        self._draw_block(draw, 40, y, f"摘要：{report['summary']}", fill=(76, 56, 36), font=body_font)
        y += 80
        self._draw_block(draw, 40, y, "市场概览", fill=(140, 80, 40), font=body_font)
        y += 40
        for line in parsed["overview_lines"][:4]:
            self._draw_block(draw, 60, y, line, fill=(60, 60, 60), font=body_font)
            y += 34
        y += 20
        self._draw_block(draw, 40, y, "个股摘要", fill=(140, 80, 40), font=body_font)
        y += 40
        for item in parsed["stocks"][:6]:
            block = f"{item['title']} | 高价值策略 {item['valuable_count']}"
            self._draw_block(draw, 60, y, block, fill=(40, 40, 40), font=body_font)
            y += 34
            for strategy in item["strategies"][:2]:
                self._draw_block(
                    draw,
                    80,
                    y,
                    f"{strategy['name']} | {strategy['status']} | {strategy['buy_zone']}",
                    fill=(80, 80, 80),
                    font=body_font,
                )
                y += 28
            y += 14
        image.save(output_path, format="PNG")

    def _render_strategy_chart(self, parsed: dict[str, Any], output_path: Path) -> None:
        titles = [item["title"][:8] for item in parsed["stocks"]] or ["N/A"]
        counts = [item["valuable_count"] for item in parsed["stocks"]] or [0]
        plt.figure(figsize=(10, 6))
        plt.bar(titles, counts, color="#bf6f3d")
        title_kwargs = {"fontproperties": self._plot_font} if self._plot_font else {}
        plt.title("高价值策略数量", **title_kwargs)
        plt.xlabel("标的", **title_kwargs)
        plt.ylabel("数量", **title_kwargs)
        if self._plot_font:
            ax = plt.gca()
            for label in ax.get_xticklabels():
                label.set_fontproperties(self._plot_font)
            for label in ax.get_yticklabels():
                label.set_fontproperties(self._plot_font)
        plt.tight_layout()
        plt.savefig(output_path, format="png")
        plt.close()

    def _render_pdf(self, report: dict[str, Any], output_path: Path) -> None:
        pdf = canvas.Canvas(str(output_path), pagesize=A4)
        width, height = A4
        y = height - 48
        pdf.setFont("STSong-Light", 18)
        pdf.drawString(40, y, report["title"])
        y -= 28
        pdf.setFont("STSong-Light", 11)
        for raw_line in str(report["content_markdown"]).splitlines():
            line = raw_line.strip() or " "
            if y < 40:
                pdf.showPage()
                pdf.setFont("STSong-Light", 11)
                y = height - 40
            pdf.drawString(40, y, line[:90])
            y -= 16
        pdf.save()

    def _parse_report(self, markdown_text: str) -> dict[str, Any]:
        overview_lines: list[str] = []
        stocks: list[dict[str, Any]] = []
        section = ""
        current_stock: dict[str, Any] | None = None
        for raw_line in markdown_text.splitlines():
            line = raw_line.strip()
            if line.startswith("## 市场概览"):
                section = "overview"
                continue
            if line.startswith("## 个股策略卡"):
                section = "stocks"
                continue
            if line.startswith("## "):
                section = ""
                current_stock = None
                continue
            if section == "overview" and line:
                overview_lines.append(line)
                continue
            if section == "stocks":
                if line.startswith("### "):
                    current_stock = {"title": line[4:], "valuable_count": 0, "strategies": []}
                    stocks.append(current_stock)
                    continue
                if current_stock is None:
                    continue
                if line.startswith("高价值策略数："):
                    match = re.search(r"(\d+)", line)
                    current_stock["valuable_count"] = int(match.group(1)) if match else 0
                    continue
                if line.startswith("- "):
                    parts = [part.strip() for part in line[2:].split("|")]
                    current_stock["strategies"].append(
                        {
                            "name": parts[0] if parts else "",
                            "status": parts[1] if len(parts) > 1 else "",
                            "buy_zone": parts[2] if len(parts) > 2 else "",
                        }
                    )
        return {"overview_lines": overview_lines, "stocks": stocks}

    def _draw_block(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        text: str,
        *,
        fill: tuple[int, int, int],
        font: Any,
    ) -> None:
        draw.text((x, y), text, fill=fill, font=font)

    def _pick_font_path(self) -> Path | None:
        candidates = (
            Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
            Path("/Library/Fonts/Arial Unicode.ttf"),
        )
        for path in candidates:
            if path.exists():
                return path
        return None

    def _load_pil_font(self, size: int) -> Any:
        if self._font_path is not None:
            try:
                return ImageFont.truetype(str(self._font_path), size=size)
            except OSError:
                pass
        return ImageFont.load_default()
