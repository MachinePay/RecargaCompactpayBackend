"""CEP opcional no cadastro do carregador: endereco e ponto no mapa."""
import httpx

from app.services import geo
from tests.test_fluxo_carga import ADMIN, client  # noqa: F401 - fixture


def test_cep_preenche_endereco_e_mapa(client, monkeypatch):
    def get_falso(url, params=None):
        if "brasilapi" in url:
            if url.endswith("00000000"):
                raise httpx.HTTPError("nao existe")
            return {"street": "Avenida Paulista", "neighborhood": "Bela Vista", "city": "Sao Paulo", "state": "SP", "location": {"coordinates": {}}}
        if "viacep" in url:
            return {"erro": True}
        if "nominatim" in url:
            return [{"lat": "-23.5614", "lon": "-46.6559"}] if params.get("street") else [{"lat": "-23.55", "lon": "-46.63"}]
        raise AssertionError(url)

    monkeypatch.setattr(geo, "_get_json", get_falso)
    r = client.get("/api/v1/admin/cep/01310-100?numero=1000", headers=ADMIN).json()
    assert r["endereco"] == "Avenida Paulista, 1000 - Bela Vista - Sao Paulo/SP"
    assert r["latitude"] == -23.5614 and r["precisao"] == "numero"
    assert client.get("/api/v1/admin/cep/123", headers=ADMIN).status_code == 422
    assert client.get("/api/v1/admin/cep/00000000", headers=ADMIN).status_code == 404

    cliente = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Dono CEP", "cliente_mercado_pago": True}, headers=ADMIN).json()
    c = client.post(
        "/api/v1/admin/carregadores",
        json={"id_hardware": "CP-CEP", "nome_local": "Paulista", "cliente_id": cliente["id"], "cep": "01310100", "numero_endereco": "1000"},
        headers=ADMIN,
    ).json()
    assert c["cep"] == "01310-100" and c["latitude"] == -23.5614 and "Paulista" in c["endereco"]
    # CEP que nao existe nao impede o cadastro.
    c2 = client.post(
        "/api/v1/admin/carregadores",
        json={"id_hardware": "CP-CEP2", "nome_local": "X", "cliente_id": cliente["id"], "cep": "00000000"},
        headers=ADMIN,
    )
    assert c2.status_code == 200 and c2.json()["latitude"] is None


def test_cep_usa_google_quando_tem_chave(client, monkeypatch):
    chamadas = []

    def get_falso(url, params=None):
        chamadas.append(url)
        if "brasilapi" in url:
            return {"street": "Avenida Cachoeira Paulista", "neighborhood": "Cidade Patriarca", "city": "Sao Paulo", "state": "SP", "location": {"coordinates": {}}}
        if "googleapis" in url:
            assert params["components"] == "country:BR|postal_code:03551000" and "Cachoeira Paulista, 17" in params["address"]
            return {"status": "OK", "results": [{"geometry": {"location": {"lat": -23.5321, "lng": -46.5047}, "location_type": "ROOFTOP"}}]}
        raise AssertionError(url)

    monkeypatch.setattr(geo, "_get_json", get_falso)
    monkeypatch.setattr(geo.settings, "GOOGLE_MAPS_API_KEY", "chave-teste")
    r = client.get("/api/v1/admin/cep/03551-000?numero=17", headers=ADMIN).json()
    assert (r["latitude"], r["longitude"], r["precisao"]) == (-23.5321, -46.5047, "numero")
    assert not any("nominatim" in u for u in chamadas)

    # Google recusou (chave errada): cai no OpenStreetMap.
    def get_recusado(url, params=None):
        if "googleapis" in url:
            return {"status": "REQUEST_DENIED", "error_message": "chave invalida"}
        if "nominatim" in url:
            return [{"lat": "-23.53", "lon": "-46.50"}]
        return get_falso(url, params)

    monkeypatch.setattr(geo, "_get_json", get_recusado)
    r = client.get("/api/v1/admin/cep/03551-000?numero=17", headers=ADMIN).json()
    assert r["latitude"] == -23.53 and r["precisao"] == "numero"
