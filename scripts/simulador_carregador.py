"""Simulador de wallbox OCPP 1.6J, para testar o servidor sem o carregador.

Uso:
    python scripts/simulador_carregador.py --id CP-BANCADA
    python scripts/simulador_carregador.py --id CP-BANCADA --url ws://192.168.0.10:8000/ocpp --sem-carro

Comporta-se como o carregador real: BootNotification, Heartbeat, aceita
RemoteStart, abre StartTransaction (se tiver "carro"), manda MeterValues a
cada 10 s (7 kW) e fecha com StopTransaction quando recebe RemoteStop.
--sem-carro: aceita o RemoteStart mas nunca comeca (como sem carro plugado).
"""
import argparse
import asyncio
import base64
import json
from datetime import datetime, timezone
from itertools import count

import websockets

POTENCIA_W = 7000
INTERVALO_MEDICAO_S = 10


def agora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Simulador:
    def __init__(self, ws, connector_id: int, sem_carro: bool):
        self.ws = ws
        self.connector_id = connector_id
        self.sem_carro = sem_carro
        self.ids = count(1)
        self.pendentes: dict[str, asyncio.Future] = {}
        self.transacao: int | None = None
        self.energia_wh = 0
        self.tarefa_medicao: asyncio.Task | None = None

    async def chamar(self, acao: str, payload: dict) -> dict:
        msg_id = f"sim{next(self.ids)}"
        futuro = asyncio.get_running_loop().create_future()
        self.pendentes[msg_id] = futuro
        await self.ws.send(json.dumps([2, msg_id, acao, payload]))
        print(f">> {acao} {payload}")
        resposta = await asyncio.wait_for(futuro, 30)
        print(f"<< {acao}: {resposta}")
        return resposta

    async def status(self, status: str) -> None:
        await self.chamar("StatusNotification", {"connectorId": self.connector_id, "errorCode": "NoError", "status": status})

    async def ouvir(self) -> None:
        async for texto in self.ws:
            msg = json.loads(texto)
            if msg[0] in (3, 4):
                futuro = self.pendentes.pop(msg[1], None)
                if futuro and not futuro.done():
                    futuro.set_result(msg[2] if msg[0] == 3 else {"erro": msg[2:]})
                continue
            _, msg_id, acao, payload = msg
            print(f"<< servidor pediu {acao} {payload}")
            resposta = {"status": "Accepted"}
            if acao == "RemoteStopTransaction" and payload.get("transactionId") != self.transacao:
                resposta = {"status": "Rejected"}
            elif acao == "GetConfiguration":
                resposta = {"configurationKey": [{"key": "HeartbeatInterval", "readonly": False, "value": "60"}], "unknownKey": []}
            elif acao not in ("RemoteStartTransaction", "RemoteStopTransaction", "ChangeConfiguration", "Reset", "TriggerMessage", "UnlockConnector"):
                await self.ws.send(json.dumps([4, msg_id, "NotImplemented", acao, {}]))
                continue
            await self.ws.send(json.dumps([3, msg_id, resposta]))
            if acao == "RemoteStartTransaction":
                asyncio.create_task(self.iniciar(payload["idTag"]))
            elif acao == "RemoteStopTransaction" and resposta["status"] == "Accepted":
                asyncio.create_task(self.parar("Remote"))

    async def iniciar(self, id_tag: str) -> None:
        await self.status("Preparing")
        if self.sem_carro:
            print("-- sem carro: fica em Preparing ate o servidor desistir")
            return
        await asyncio.sleep(2)  # "cliente plugando"
        conf = await self.chamar(
            "StartTransaction",
            {"connectorId": self.connector_id, "idTag": id_tag, "meterStart": self.energia_wh, "timestamp": agora_iso()},
        )
        if conf.get("idTagInfo", {}).get("status") != "Accepted":
            await self.status("Available")
            return
        self.transacao = conf["transactionId"]
        await self.status("Charging")
        self.tarefa_medicao = asyncio.create_task(self.medir())

    async def medir(self) -> None:
        while self.transacao is not None:
            await asyncio.sleep(INTERVALO_MEDICAO_S)
            if self.transacao is None:
                break
            self.energia_wh += POTENCIA_W * INTERVALO_MEDICAO_S // 3600
            await self.chamar(
                "MeterValues",
                {
                    "connectorId": self.connector_id,
                    "transactionId": self.transacao,
                    "meterValue": [{"timestamp": agora_iso(), "sampledValue": [{"value": str(self.energia_wh), "unit": "Wh"}]}],
                },
            )

    async def parar(self, motivo: str) -> None:
        transacao, self.transacao = self.transacao, None
        if transacao is None:
            return
        await self.chamar("StopTransaction", {"transactionId": transacao, "meterStop": self.energia_wh, "timestamp": agora_iso(), "reason": motivo})
        await self.status("Finishing")
        await self.status("Available")
        print("-- energia cortada")

    async def heartbeat(self, intervalo: int) -> None:
        while True:
            await asyncio.sleep(intervalo)
            await self.chamar("Heartbeat", {})


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default="CP-BANCADA")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ocpp")
    parser.add_argument("--senha", default="")
    parser.add_argument("--connector", type=int, default=1)
    parser.add_argument("--sem-carro", action="store_true")
    args = parser.parse_args()

    headers = {}
    if args.senha:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{args.id}:{args.senha}".encode()).decode()
    async with websockets.connect(f"{args.url.rstrip('/')}/{args.id}", subprotocols=["ocpp1.6"], additional_headers=headers) as ws:
        sim = Simulador(ws, args.connector, args.sem_carro)
        ouvinte = asyncio.create_task(sim.ouvir())
        boot = await sim.chamar("BootNotification", {"chargePointVendor": "Simulador", "chargePointModel": "WB-7kW", "firmwareVersion": "sim-1.0"})
        await sim.status("Available")
        asyncio.create_task(sim.heartbeat(int(boot.get("interval") or 60)))
        await ouvinte


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
