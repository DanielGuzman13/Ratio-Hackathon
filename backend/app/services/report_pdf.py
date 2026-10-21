"""Informe de diagnóstico en PDF (se adjunta al correo que recibe la persona).

Usa fpdf2 (Python puro). Si el sistema tiene Arial (Windows) o DejaVu (Linux) los usa para
cubrir cualquier carácter; si no, cae a Helvetica y reemplaza los pocos símbolos que no soporta.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fpdf import FPDF

INK = (29, 42, 68)
INK_2 = (74, 85, 104)
LINE = (221, 213, 198)
PAPER = (246, 242, 234)
STATUS = {"fortaleza": "Fortaleza", "en_desarrollo": "En desarrollo", "vacio": "Vacío conceptual",
          "sin_evidencia": "Sin evidencia"}
PRIORITY = {"alta": "Prioridad alta", "media": "Prioridad media", "baja": "Prioridad baja"}
LOGO = Path(__file__).resolve().parents[3] / "frontend" / "img" / "ratio-icon.png"
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]
LATIN1_FIX = {"—": "-", "–": "-", "…": "...", "≈": "~", "“": '"', "”": '"', "‘": "'", "’": "'", "•": "·", "✓": "v"}


def local_time(iso: str | None, tz: str = "America/Bogota") -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 - sin base de zonas horarias: se deja en UTC
        pass
    return dt.strftime("%d/%m/%Y, %I:%M %p").replace("AM", "a. m.").replace("PM", "p. m.")


class _Doc(FPDF):
    def __init__(self) -> None:
        super().__init__(format="Letter")
        self.unicode_font = False
        for regular, bold in FONT_CANDIDATES:
            if Path(regular).exists() and Path(bold).exists():
                self.add_font("Body", "", regular)
                self.add_font("Body", "B", bold)
                self.unicode_font = True
                break
        self.family = "Body" if self.unicode_font else "Helvetica"
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, 18)

    def clean(self, text) -> str:
        text = str(text if text is not None else "")
        if self.unicode_font:
            return text
        for bad, good in LATIN1_FIX.items():
            text = text.replace(bad, good)
        return text.encode("latin-1", "replace").decode("latin-1")

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(self.family, "", 8)
        self.set_text_color(*INK_2)
        self.cell(0, 6, self.clean(f"Ratio · Informe de diagnóstico jurídico · página {self.page_no()}"), align="C")

    # --- bloques ---
    def h2(self, text: str) -> None:
        self.ln(4)
        self.set_font(self.family, "B", 13)
        self.set_text_color(*INK)
        self.cell(0, 8, self.clean(text), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*INK)
        self.set_line_width(0.6)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2.5)

    def para(self, text: str, *, bold_label: str = "", size: float = 10, color=INK) -> None:
        self.set_text_color(*color)
        self.set_font(self.family, "", size)
        md = f"**{self.clean(bold_label)}** " if bold_label else ""
        body = self.clean(text).replace("**", "")
        self.multi_cell(0, size * 0.5, md + body, markdown=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(0.8)

    def grid(self, header: list[str], rows: list[list[str]], widths: list[float]) -> None:
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.set_fill_color(255, 255, 255)
        self.set_font(self.family, "", 9)
        self.set_text_color(*INK)
        with self.table(col_widths=widths, line_height=4.6, padding=1.4, headings_style=_heading_style(self.family),
                        first_row_as_headings=True, borders_layout="ALL", text_align="LEFT") as table:
            head = table.row()
            for h in header:
                head.cell(self.clean(h))
            for r in rows:
                row = table.row()
                for value in r:
                    row.cell(self.clean(value))
        self.ln(2)


def _heading_style(family: str):
    from fpdf.fonts import FontFace
    return FontFace(family=family, emphasis="BOLD", color=INK, fill_color=PAPER)


def build_pdf(payload: dict) -> bytes:
    doc = _Doc()
    doc.add_page()
    profile = payload.get("profile", {})
    result = payload.get("result", {})
    g = result.get("global", {})

    # Encabezado
    top = doc.get_y()
    if LOGO.exists():
        doc.image(str(LOGO), x=doc.l_margin, y=top, w=15, h=15)
    doc.set_xy(doc.l_margin + 19, top)
    doc.set_text_color(*INK)
    doc.set_font(doc.family, "B", 17)
    doc.cell(0, 8, doc.clean("Informe de diagnóstico jurídico"), new_x="LMARGIN", new_y="NEXT")
    doc.set_x(doc.l_margin + 19)
    doc.set_font(doc.family, "", 9.5)
    doc.set_text_color(*INK_2)
    doc.multi_cell(0, 4.6, doc.clean(
        f"{profile.get('nombre', '')} · {profile.get('email', '')}\n"
        f"{payload.get('area_name', '')} · {profile.get('perfil_label', '')} · {profile.get('objetivo_label', '')}\n"
        f"Presentado el {local_time(payload.get('created_at'))}"), new_x="LMARGIN", new_y="NEXT")
    doc.ln(2)

    # Resultado
    doc.h2("Resultado")
    mastery = g.get("mastery")
    doc.grid(["Nivel global", "Enfoque", "Dominio"],
              [[g.get("level_name", "—"), g.get("approach_label", "—"),
                f"{round(mastery * 100)} %" if isinstance(mastery, (int, float)) else "—"]], [60, 60, 54])
    subs = [s for s in result.get("subtopics", {}).values() if s.get("status") != "sin_evidencia"]
    doc.grid(["Tema", "Estado", "Nivel alcanzado", "Aciertos"],
              [[s.get("name", ""), STATUS.get(s.get("status"), s.get("status", "")), s.get("level_name") or "—",
                f"{s.get('correct', 0)}/{s.get('answered', 0)}"] for s in subs], [78, 38, 36, 22])

    # Ruta
    doc.h2("Ruta de aprendizaje")
    for item in payload.get("route", []):
        doc.set_font(doc.family, "B", 11)
        doc.set_text_color(*INK)
        doc.multi_cell(0, 5.5, doc.clean(f"{item.get('step')}. {item.get('subtopic')}  "
                                         f"({PRIORITY.get(item.get('priority'), '')} · {item.get('hours')} h)"),
                       new_x="LMARGIN", new_y="NEXT")
        doc.para(item.get("objective", ""))
        doc.para(", ".join(item.get("focus_concepts") or []), bold_label="Enfócate en:")
        doc.para("; ".join(s.get("citation", "") for s in item.get("sources") or []), bold_label="Fuentes:")
        if item.get("teacher_note"):
            doc.para(item["teacher_note"], bold_label="Nota del administrador:")
        for c in item.get("admin_comments") or []:
            doc.para(c.get("comment", ""), bold_label="Revisión:")
        doc.para((item.get("justification") or {}).get("text", ""), bold_label="Por qué:", size=9, color=INK_2)
        doc.set_draw_color(*LINE)
        doc.line(doc.l_margin, doc.get_y() + 1, doc.w - doc.r_margin, doc.get_y() + 1)
        doc.ln(3)

    # Preguntas
    questions = payload.get("questions") or []
    if questions:
        doc.h2("Preguntas respondidas")
        doc.grid(["#", "Tema", "Pregunta", "Resultado"],
                  [[str(q.get("seq")), q.get("subtopic", ""), q.get("stem", ""), "Acierto" if q.get("correct") else "Error"]
                   for q in questions], [8, 36, 106, 24])

    review = payload.get("review", {})
    doc.ln(2)
    if review.get("status") == "no_requiere":
        closing = "Informe generado automáticamente: no tenía elementos que requirieran revisión."
    else:
        closing = (f"Revisado y aprobado por {review.get('reviewed_by') or 'el administrador'} "
                   f"({local_time(review.get('reviewed_at'))}).")
        if review.get("notes"):
            closing += f" Observaciones: {review['notes']}"
    doc.para(closing + " Ratio evalúa y orienta; no reemplaza la asesoría de un abogado.", size=8.5, color=INK_2)
    return bytes(doc.output())
