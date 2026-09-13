import csv
import datetime
from decimal import Decimal, InvalidOperation


def abrir_csv(path):
    """Abre un CSV probando encodings comunes en exportaciones SAP/Excel en Windows."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            fh = open(path, encoding=encoding, newline="")
            fh.read(1024)
            fh.seek(0)
            return fh
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(f"No se pudo determinar el encoding de {path}")


def leer_filas(path):
    """Generador de dicts (DictReader) para un CSV, cerrando el archivo al terminar."""
    fh = abrir_csv(path)
    try:
        reader = csv.DictReader(fh)
        for row in reader:
            yield row
    finally:
        fh.close()


def texto(valor):
    if valor is None:
        return ""
    valor = valor.strip()
    if valor.lower() in ("", "nan", "none", "null"):
        return ""
    return valor


def codigo(valor):
    """Limpia códigos que llegaron con '.0' final por haber pasado por Excel/pandas."""
    valor = texto(valor)
    if valor.endswith(".0") and valor[:-2].lstrip("-").isdigit():
        return valor[:-2]
    return valor


def entero(valor):
    valor = texto(valor)
    if not valor:
        return None
    try:
        return int(float(valor))
    except ValueError:
        return None


def decimal(valor):
    valor = texto(valor)
    if not valor:
        return None
    valor = valor.replace(",", "")
    try:
        return Decimal(valor)
    except InvalidOperation:
        return None


def fecha(valor):
    valor = texto(valor)
    if not valor:
        return None
    valor = valor.split(" ")[0]
    try:
        return datetime.datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(valor, fmt).date()
        except ValueError:
            continue
    return None
