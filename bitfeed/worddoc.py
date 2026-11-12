"""Write a small .docx (headings, paragraphs, hyperlinks) with the standard library only."""
from xml.sax.saxutils import escape, quoteattr
import zipfile

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/>
<w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="200"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="320" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="000000"/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Note"><w:name w:val="Note"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="666666"/><w:sz w:val="18"/></w:rPr></w:style>
<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>
</w:styles>"""


class Document:
    def __init__(self):
        self.body, self.links = [], []

    def _run(self, text, bold=False):
        props = "<w:rPr><w:b/></w:rPr>" if bold else ""
        return f'<w:r>{props}<w:t xml:space="preserve">{escape(str(text))}</w:t></w:r>'

    def _para(self, inner, style=None, indent=False, keep_next=False):
        props = ""
        if style or indent or keep_next:
            props = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + (
                '<w:ind w:left="360" w:hanging="200"/>' if indent else "") + ('<w:keepNext/>' if keep_next else '') + "</w:pPr>"
        self.body.append(f"<w:p>{props}{inner}</w:p>")

    def title(self, text):
        self._para(self._run(text), "Title")

    def heading(self, text, level=1):
        self._para(self._run(text), "Heading1" if level == 1 else "Heading2")

    def paragraph(self, text, bold=False, note=False):
        self._para(self._run(text, bold), "Note" if note else None)

    def item(self, title, url=None, detail="", keep_next=False):
        """One bulleted entry: linked title, then grey detail text."""
        parts = [self._run("• ")]
        if url:
            self.links.append(url)
            rid = f"rIdLink{len(self.links)}"
            parts.append(f'<w:hyperlink r:id="{rid}"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
                         f'<w:t xml:space="preserve">{escape(str(title))}</w:t></w:r></w:hyperlink>')
        else:
            parts.append(self._run(title))
        if detail:
            parts.append(f'<w:r><w:rPr><w:color w:val="666666"/><w:sz w:val="18"/></w:rPr>'
                         f'<w:t xml:space="preserve">  {escape(str(detail))}</w:t></w:r>')
        self._para("".join(parts), indent=True, keep_next=keep_next)

    def save(self, path):
        document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
                    + "".join(self.body) +
                    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1200" w:right="1100" '
                    'w:bottom="1200" w:left="1100" w:header="600" w:footer="600" w:gutter="0"/></w:sectPr>'
                    "</w:body></w:document>")
        rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>']
        for index, url in enumerate(self.links, 1):
            rels.append(f'<Relationship Id="rIdLink{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        f'relationships/hyperlink" Target={quoteattr(url)} TargetMode="External"/>')
        rels.append("</Relationships>")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("_rels/.rels", ROOT_RELS)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/styles.xml", STYLES)
            archive.writestr("word/_rels/document.xml.rels", "".join(rels))
