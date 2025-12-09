from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from weasyprint import HTML, CSS
from weasyprint.text.fonts import FontConfiguration

from db import Report, update_report_pdf_path
from holidays_manager import get_holiday_emoji

logger = logging.getLogger(__name__)

# PDF styles
CSS_STYLE = """
@page {
    size: A4;
    margin: 2cm;
    @bottom-center {
        content: counter(page) "/" counter(pages);
        font-family: 'Helvetica', sans-serif;
        font-size: 9pt;
        color: #888;
    }
}

body {
    font-family: 'Helvetica', 'Arial';
    color: #333;
    line-height: 1.6;
    font-size: 11pt;
    margin: 0;
    padding: 0;
}

h1 {
    color: #2c3e50;
    font-size: 24pt;
    border-bottom: 2px solid #3498db;
    padding-bottom: 10px;
    margin-bottom: 20px;
    text-align: center;
}

h2 {
    color: #2980b9;
    font-size: 16pt;
    margin-top: 30px;
    margin-bottom: 15px;
    padding-left: 10px;
    border-left: 5px solid #630fb8;
}

.date-range {
    text-align: center;
    color: #7f8c8d;
    font-style: italic;
    margin-bottom: 40px;
    font-size: 10pt;
}

.summary-box {
    background-color: #f8f9fa;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 15px;
    margin-bottom: 20px;
    box-shadow: 2px 2px 5px rgba(0,0,0,0.05);
}

.summary-item {
    margin-bottom: 8px;
    padding-left: 15px;
    position: relative;
}

.summary-item::before {
    content: "•";
    position: absolute;
    left: 0;
    color: #3498db;
    font-weight: bold;
}

.section-block {
    margin-bottom: 25px;
}

.news-item {
    margin-bottom: 12px;
    padding: 10px;
    background-color: #fff;
    border-bottom: 1px solid #eee;
}

.priority-high {
    border-left: 4px solid #e67e22;
    padding-left: 10px;
}

.priority-medium {
    border-left: 4px solid #f1c40f;
    padding-left: 10px;
}

.priority-low {
    border-left: 4px solid #95a5a6;
    padding-left: 10px;
}

.ticker {
    font-weight: bold;
    color: #2c3e50;
    background-color: #ecf0f1;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
    margin-right: 5px;
}

.direction-bullish {
    color: #27ae60;
    font-weight: bold;
}

.direction-bearish {
    color: #c0392b;
    font-weight: bold;
}

.direction-neutral {
    color: #7f8c8d;
    font-weight: bold;
}

.asset-card {
    border: 1px solid #ddd;
    border-radius: 6px;
    padding: 12px;
    margin-bottom: 10px;
    page-break-inside: avoid;
}

.watermark {
    position: fixed;
    bottom: 10px;
    right: 10px;
    color: #dfe6e9;
    font-size: 30pt;
    transform: rotate(-45deg);
    z-index: -1;
}

.footer {
    margin-top: 50px;
    text-align: center;
    font-size: 9pt;
    color: #999da1;
    border-top: 1px solid #cfd3d4;
    padding-top: 10px;
}
"""

def _build_html_content(data: Dict, period_str: str, labels: Dict[str, str]) -> str:
    # Parse report data
    summary  = data.get("summary", [])
    positive = data.get("positive", [])
    negative = data.get("negative", [])
    macro    = data.get("macro", [])
    assets   = data.get("assets", [])
    
    lbl = lambda k, d: labels.get(k, d)

    # Build HTML
    html_parts = []
    
    # Header
    html_parts.append(f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>{CSS_STYLE}</style>
    </head>
    <body>
    <h1>{lbl("digest_title", "Crypto Digest")}</h1>
        <div class="date-range">{period_str}</div>
    """)

    # Summary
    if summary:
        html_parts.append(f'<h2>{lbl("summary", "Summary")}</h2>')
        html_parts.append('<div class="summary-box">')
        for item in summary:
            html_parts.append(f'<div class="summary-item">{item}</div>')
        html_parts.append('</div>')

    # Assets
    if assets:
        html_parts.append(f'<h2>{lbl("assets", "Promising Assets")}</h2>')
        for item in assets:
            ticker = item.get("ticker", "")
            direction = item.get("direction", "neutral").lower()
            priority = item.get("priority", "medium").lower()
            reason = item.get("reason", "")
            
            html_parts.append(f"""
            <div class="asset-card priority-{priority}">
                <div style="font-size: 1.1em; margin-bottom: 5px;">
                    <span class="ticker">{ticker}</span>
                    <span class="direction-{direction}">{direction.upper()}</span>
                </div>
                <div>{reason}</div>
            </div>
            """)

    # Positive News
    if positive:
        html_parts.append(f'<h2>{lbl("positive", "Positive News")}</h2>')
        for item in positive:
            html_parts.append(_render_news_item(item))

    # Negative News
    if negative:
        html_parts.append(f'<h2>{lbl("negative", "Negative Factors")}</h2>')
        for item in negative:
            html_parts.append(_render_news_item(item))

    # Macro
    if macro:
        html_parts.append(f'<h2>{lbl("macro", "Macro Events")}</h2>')
        for item in macro:
            html_parts.append(_render_news_item(item))
             
    # Footer
    html_parts.append(f"""
        <div class="footer">
            {lbl("footer", "Generated by Crypto AI Digest Bot")} (<a href="https://t.me/CryptoOwnAIDigestBot" style="text-decoration: none; color: #3498db; font-weight: bold;">@CryptoOwnAIDigestBot</a>)
        </div>
    </body>
    </html>
    """)
    
    return "\n".join(html_parts)

def _render_news_item(item: Dict) -> str:
    text = item.get("text", "")
    priority = item.get("priority", "medium").lower()
    tickers = item.get("tickers", [])
    
    tickers_html = ""
    if tickers:
        tickers_html = " ".join([f'<span class="ticker">{t}</span>' for t in tickers]) + " "
        
    return f"""
    <div class="news-item priority-{priority}">
        <div>{tickers_html}{text}</div>
    </div>
    """

def generate_pdf_report(
    report: Report,
    labels: Optional[Dict[str, str]] = None,
    lang_suffix: str = "",
    save_to_db: bool = True
) -> Optional[str]:
    """
    Generates a PDF for the given report.
    Returns the absolute path to the generated PDF file.
    """
    try:
        # Load JSON content
        data = json.loads(report.json_content)
        
        # Format Period
        period_str = f"{report.period_start_utc.strftime('%Y-%m-%d %H:%M')} - {report.period_end_utc.strftime('%Y-%m-%d %H:%M')} (UTC)"
        
        # Check for holiday
        emoji = get_holiday_emoji(report.period_end_utc)
        if emoji:
            if labels is None:
                labels = {}
            # Add emoji with cross-platform font fallback
            base_title = labels.get("digest_title", "Crypto Digest")
            labels["digest_title"] = (
                f'<span style="font-family: \'Segoe UI Emoji\', \'Segoe UI Symbol\', \'Apple Color Emoji\', \'Noto Color Emoji\', sans-serif;">'
                f'{emoji}</span> {base_title}'
            )

        # Build HTML
        html_content = _build_html_content(data, period_str, labels or {})
        
        # Define output path
        timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
        suffix = f"_{lang_suffix}" if lang_suffix else ""
        filename = f"digest_{report.id}_{report.kind}{suffix}_{timestamp_str}_UTC.pdf"
        output_dir = os.path.join(os.path.dirname(__file__), "reports_pdf")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, filename)
        
        # Generate PDF
        font_config = FontConfiguration()
        HTML(string=html_content).write_pdf(output_path, font_config=font_config)
        
        logger.info(f"PDF generated successfully: {output_path}")
        
        # Update DB if requested or if it's the primary report
        if save_to_db or not lang_suffix:
            update_report_pdf_path(report.id, output_path)
        
        return output_path
        
    except Exception as e:
        logger.exception(f"Failed to generate PDF for report {report.id}: {e}")
        return None


def cleanup_old_reports(max_age_hours: int = 24) -> int:
    """
    Delete PDF files older than max_age_hours from the reports_pdf directory.
    Returns the number of deleted files.
    """
    count = 0
    try:
        output_dir = os.path.join(os.path.dirname(__file__), "reports_pdf")
        if not os.path.exists(output_dir):
            return 0
            
        now = datetime.now().timestamp()
        cutoff = now - (max_age_hours * 3600)
        
        for filename in os.listdir(output_dir):
            if not filename.endswith(".pdf"):
                continue
                
            filepath = os.path.join(output_dir, filename)
            try:
                mtime = os.path.getmtime(filepath)
                if mtime < cutoff:
                    os.remove(filepath)
                    count += 1
                    logger.info(f"Deleted old PDF: {filename}")
            except Exception as ex:
                logger.warning(f"Failed to delete {filename}: {ex}")
                
    except Exception as e:
        logger.exception(f"Error during PDF cleanup: {e}")
        
    return count
