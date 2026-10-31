"""Text checks shared by every file type: rights marks, personal data, language.

Rights marks keep a few short snippets (they are the evidence a reviewer needs). Personal data keeps counts per kind
only, never the values, so the scan output itself holds no PII.
"""
import re
from collections import Counter

RIGHTS = [
    ("copyright", r"copyright|\(c\)\s*\d{4}|©\s*(?:\d{4}|[A-Z][a-z])|all rights reserved|todos os direitos|derechos reservados|tous droits"),
    ("property_of", r"(?:is the |exclusive )?property of|propriedade d[aoe]|propiedad de|propriété de|eigentum"),
    ("no_reproduce", r"not (?:to )?be (?:reproduced|copied|distributed|disclosed|used)|"
                     r"do not (?:copy|distribute|reproduce|share)|without (?:the )?(?:prior )?(?:written )?(?:permission|consent)|"
                     r"n[ãa]o (?:pode|deve) ser (?:reproduzid|copiad|divulgad)|prohibida su reproducci|"
                     r"not for (?:resale|redistribution|ai|machine learning|training)|no ai training|not for ai"),
    ("confidential", r"confidential|proprietary|internal use only|confidencial|vertraulich|riservato"),
    ("watermark", r"(?<![A-Za-z])watermark(?!manager)|marca d'?[aá]gua|sample only|for review only|evaluation copy|educational version|"
                  r"student version|produced by an autodesk student|for preview|draft - not for construction"),
    ("demo_synthetic", r"\bdemo\b|synthetic|dummy data|fake data|(?<![A-Za-z])placeholder\b|lorem ipsum"),
    ("license", r"licen[cs]ed to|license agreement|registered to|serial number"),
]
RIGHTS_RX = [(name, re.compile(rx, re.I)) for name, rx in RIGHTS]

PII = [
    ("email", r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{2,63})+"),
    ("phone", r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[ .-]?)\d{3,4}[ .-]\d{4}(?![\w.])"),
    ("india_pan", r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    ("india_aadhaar", r"\b\d{4}\s\d{4}\s\d{4}\b"),
    ("brazil_cpf", r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    ("brazil_cnpj", r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b"),
    ("us_ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("windows_user_path", r"[A-Z]:\\(?:Users|Documents and Settings)\\[^\\\s]{2,40}"),
    ("network_path", r"\\\\[A-Za-z0-9_.-]{2,40}\\[A-Za-z0-9_$ .-]{1,60}"),
]
PII_RX = [(name, re.compile(rx)) for name, rx in PII]

# Software vendors' own boilerplate (embedded in every file their tool writes) is not a rights mark on the data.
VENDOR_RX = re.compile(r"mcneel|autodesk|parametric technology|hewlett-packard|icc_profile|srgb|iec ?61966|dassault|solidworks corp|siemens|bentley|trimble|sketchup|adobe|microsoft|"
                       r"gemvision|stuller|parasolid|spatial corp|open design alliance|graebert|ptc inc|"
                       r"hexagon|esri|apple|google|mozilla|unicode|pdf-?lib|itext|ghostscript", re.I)
TEXT_ONLY_PII = {"phone", "india_pan", "india_aadhaar", "us_ssn"}  # too noisy in strings pulled from binaries

STOP = {
    "en": "the and of to in for with is on that this by be are from as at drawing scale sheet",
    "pt": "de da do das dos em para com uma por que não são pelo pela planta escala folha projeto",
    "es": "de la el los las en para con una por que del plano escala hoja proyecto",
    "it": "di il della delle per con una che del dei pianta scala tavola progetto",
    "fr": "de la le les des pour avec une par que du plan échelle feuille projet",
    "de": "der die das und mit für eine von den im ist maßstab blatt zeichnung",
    "ru": "и в на с по для не что это из",
    "tr": "ve bir için ile bu da olarak proje ölçek",
}
STOP_SETS = {k: set(v.split()) for k, v in STOP.items()}
SCRIPTS = [("zh", r"[一-鿿]"), ("ja", r"[぀-ヿ]"), ("ko", r"[가-힯]"), ("he", r"[֐-׿]"),
           ("ar", r"[؀-ۿ]"), ("ru", r"[Ѐ-ӿ]"), ("hi", r"[ऀ-ॿ]"), ("th", r"[฀-๿]")]


def language(text):
    sample = text[:300000]
    for lang, rx in SCRIPTS:
        if len(re.findall(rx, sample)) > 30:
            return lang
    words = Counter(w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{2,}", sample))
    if sum(words.values()) < 40:
        return None
    scores = {lang: sum(words[w] for w in s) for lang, s in STOP_SETS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 5 else None


def check(text, binary=False):
    """binary=True: the text was pulled as strings from a binary file, so skip the noisy checks."""
    out = {}
    rights = {}
    snippets = []
    vendor = 0
    for name, rx in RIGHTS_RX:
        n = 0
        for m in rx.finditer(text):
            s = text[max(0, m.start() - 80): m.end() + 80]
            if VENDOR_RX.search(s):
                vendor += 1
                continue
            n += 1
            if n <= 2:
                snippets.append(f"[{name}] " + re.sub(r"\s+", " ", s).strip()[:180])
        if n:
            rights[name] = n
    if vendor:
        out["vendor_marks"] = vendor
    if rights:
        out["rights"] = rights
        out["rights_snippets"] = snippets[:6]
    pii = {name: len(rx.findall(text)) for name, rx in PII_RX if not (binary and name in TEXT_ONLY_PII)}
    if "us_ssn" in pii and not re.search(r"social security|\bssn\b", text, re.I):
        del pii["us_ssn"]  # part numbers share the ###-##-#### shape
    if "email" in pii:
        pii["email"] = sum(1 for e in PII_RX[0][1].findall(text) if not VENDOR_RX.search(e))
    pii = {k: v for k, v in pii.items() if v}
    if pii:
        out["pii"] = pii
    lang = None if binary else language(text)
    if lang:
        out["lang"] = lang
    return out
