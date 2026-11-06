"""Endereco e coordenadas a partir do CEP, para o mapa (opcional no cadastro
do carregador).

1. BrasilAPI (CEP v2): endereco e, quando tem, coordenadas da rua.
2. ViaCEP: reserva para o endereco se a BrasilAPI cair.
3. Google Geocoding (com GOOGLE_MAPS_API_KEY): coordenadas pelo numero da casa.
4. Nominatim (OpenStreetMap): reserva sem chave ou se o Google falhar; sem
   achar, o centro da cidade (precisao "cidade").
Tudo e' aproximado: o ponto exato continua sendo marcado pelo celular.
"""
import logging
import re

import httpx
from fastapi import HTTPException

from app.core.config import settings

AGENTE = "CompactRecarga/1.0 (painel de carregadores)"


def _get_json(url: str, params: dict | None = None):
    resposta = httpx.get(url, params=params, headers={"User-Agent": AGENTE}, timeout=10)
    resposta.raise_for_status()
    return resposta.json()


def normalizar_cep(cep: str) -> str:
    digitos = re.sub(r"\D", "", cep or "")
    if len(digitos) != 8:
        raise HTTPException(status_code=422, detail="CEP invalido: use 8 numeros")
    return digitos


def _coordenada(valor) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _nominatim(params: dict) -> tuple[float, float] | None:
    try:
        achados = _get_json("https://nominatim.openstreetmap.org/search", {"format": "json", "limit": 1, "countrycodes": "br", **params})
    except (httpx.HTTPError, ValueError):
        return None
    if not achados:
        return None
    lat, lng = _coordenada(achados[0].get("lat")), _coordenada(achados[0].get("lon"))
    return (lat, lng) if lat is not None and lng is not None else None


# location_type do Google -> nossa precisao.
PRECISAO_GOOGLE = {"ROOFTOP": "numero", "RANGE_INTERPOLATED": "numero", "GEOMETRIC_CENTER": "rua", "APPROXIMATE": "cidade"}


def _google(endereco: str, cep: str) -> tuple[float, float, str] | None:
    if not settings.GOOGLE_MAPS_API_KEY:
        return None
    try:
        dados = _get_json(
            "https://maps.googleapis.com/maps/api/geocode/json",
            {"address": endereco, "components": f"country:BR|postal_code:{cep}", "language": "pt-BR", "key": settings.GOOGLE_MAPS_API_KEY},
        )
    except (httpx.HTTPError, ValueError):
        return None
    if dados.get("status") != "OK" or not dados.get("results"):
        # REQUEST_DENIED = chave sem a Geocoding API ativada ou restrita por dominio.
        logging.warning("[CEP] Google Geocoding: %s %s", dados.get("status"), dados.get("error_message") or "")
        return None
    geometria = dados["results"][0].get("geometry") or {}
    local = geometria.get("location") or {}
    lat, lng = _coordenada(local.get("lat")), _coordenada(local.get("lng"))
    if lat is None or lng is None:
        return None
    return lat, lng, PRECISAO_GOOGLE.get(geometria.get("location_type"), "rua")


def buscar_cep(cep: str, numero: str | None = None) -> dict:
    cep = normalizar_cep(cep)
    rua = bairro = cidade = uf = None
    latitude = longitude = None
    try:
        dados = _get_json(f"https://brasilapi.com.br/api/cep/v2/{cep}")
        rua, bairro, cidade, uf = dados.get("street"), dados.get("neighborhood"), dados.get("city"), dados.get("state")
        coords = (dados.get("location") or {}).get("coordinates") or {}
        latitude, longitude = _coordenada(coords.get("latitude")), _coordenada(coords.get("longitude"))
    except (httpx.HTTPError, ValueError):
        try:
            dados = _get_json(f"https://viacep.com.br/ws/{cep}/json/")
            if not dados.get("erro"):
                rua, bairro, cidade, uf = dados.get("logradouro"), dados.get("bairro"), dados.get("localidade"), dados.get("uf")
        except (httpx.HTTPError, ValueError):
            logging.warning("[CEP] BrasilAPI e ViaCEP falharam para %s", cep)
    if not cidade:
        raise HTTPException(status_code=404, detail="CEP nao encontrado")

    precisao = "rua" if latitude is not None else None
    google = _google(", ".join(filter(None, [f"{rua}, {numero}" if rua and numero else rua, bairro, cidade, uf])), cep)
    if google and google[2] != "cidade":
        latitude, longitude, precisao = google
    # Com numero, a busca na rua e' mais precisa que a coordenada do CEP.
    elif rua and (numero or latitude is None):
        achado = _nominatim({"street": f"{numero} {rua}".strip() if numero else rua, "city": cidade, "state": uf, "postalcode": cep})
        if achado is None and numero:
            achado = _nominatim({"street": rua, "city": cidade, "state": uf})
        if achado:
            latitude, longitude = achado
            precisao = "numero" if numero else "rua"
    if latitude is None:
        achado = _nominatim({"city": cidade, "state": uf})
        if achado:
            latitude, longitude = achado
            precisao = "cidade"

    linha_rua = ", ".join(filter(None, [rua, numero]))
    endereco = " - ".join(filter(None, [linha_rua, bairro, f"{cidade}/{uf}" if uf else cidade]))
    return {
        "cep": f"{cep[:5]}-{cep[5:]}",
        "rua": rua,
        "bairro": bairro,
        "cidade": cidade,
        "uf": uf,
        "endereco": endereco,
        "latitude": latitude,
        "longitude": longitude,
        "precisao": precisao,
    }
