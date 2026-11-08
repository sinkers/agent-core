"""The brand pack: how documents, decks, workbooks and plan overviews look. A folder with `brand.yaml` plus the
files it names (Office templates, a wordmark image). Found at BRAND_DIR, else org.yaml's brand_dir, else the
TEMPLATES_DIR the image sets. Without one, a neutral built-in brand applies (no templates: documents are styled in
code, decks use python-pptx's plain layouts).

    name: Example
    palette:            # hex, no '#'
      ink: 222222       # text
      primary: 1F4E79   # structure: table headers, rules, gate badges
      accent: C55A11    # the one highlight per page
      muted: 7F7F7F     # secondary text
      tint: E8EEF4      # light fills
      page: FFFFFF      # deck page ground
      rule: D0D0D0      # thin rules
    fonts: {heading: Calibri, body: Calibri, mono: Consolas}
    classification: Confidential          # the footer's word on documents
    website: example.com
    table_style: ""                       # a Word table style in the template ("" = styled in code)
    wordmark: wordmark.png                # in this folder; "" for none
    templates: {word: "", letter: "", excel: "", deck: ""}   # file names in this folder
    deck_layouts: {title: Title Slide, bullets: Title and Content, ...}  # layout names in the deck template
    letterhead_placeholders: {email: "name@example.com", phone: "+00 000 000 000"}
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .org import org


class Palette(BaseModel):
    ink: str = "222222"
    primary: str = "1F4E79"
    accent: str = "C55A11"
    muted: str = "7F7F7F"
    tint: str = "E8EEF4"
    page: str = "FFFFFF"
    rule: str = "D0D0D0"
    rule_doc: str = ""  # thin rules in Word documents, if different from `rule`


class Fonts(BaseModel):
    heading: str = "Calibri"
    body: str = "Calibri"
    mono: str = "Consolas"


class Templates(BaseModel):
    word: str = ""
    letter: str = ""
    excel: str = ""
    deck: str = ""


class Brand(BaseModel):
    name: str = ""  # shown in document footers and headers; defaults to the organisation's name
    palette: Palette = Field(default_factory=Palette)
    fonts: Fonts = Field(default_factory=Fonts)
    classification: str = "Confidential"
    website: str = ""
    table_style: str = ""
    wordmark: str = ""
    templates: Templates = Field(default_factory=Templates)
    deck_layouts: dict[str, str] = Field(default_factory=dict)
    letterhead_placeholders: dict[str, str] = Field(default_factory=dict)
    dir: str = ""

    def file(self, name: str) -> Path | None:
        """A file in the brand folder, or None if the brand doesn't have it."""
        if not name or not self.dir:
            return None
        p = Path(self.dir) / name
        return p if p.exists() else None


def brand_dir() -> Path | None:
    for candidate in (os.environ.get("BRAND_DIR"), org().brand_dir, os.environ.get("TEMPLATES_DIR")):
        if candidate and (Path(candidate) / "brand.yaml").exists():
            return Path(candidate)
    return None


@lru_cache(maxsize=1)
def brand() -> Brand:
    d = brand_dir()
    if not d:
        return Brand(name=org().name)
    b = Brand.model_validate(yaml.safe_load((d / "brand.yaml").read_text()) or {})
    b.dir = str(d)
    b.name = b.name or org().name
    return b
