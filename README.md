# CompactPay Recarga - backend

Servidor OCPP 1.6J + API do totem e do painel. O carregador conecta por
WebSocket, o tablet vende pacotes de tempo e o servidor liga e desliga a
recarga.

FastAPI, SQLAlchemy sincrono e Postgres em producao, no mesmo padrao do
backend CompactPay. O fluxo completo esta no README do projeto.

## Rodar local

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env    # troque ADMIN_API_KEY
.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Testes: `.venv/Scripts/python -m pytest -q`

## Deploy na Render

O `render.yaml` cria o servidor e o Postgres juntos.

1. Render > **New > Blueprint** > escolha este repositorio > **Apply**.
2. Quando pedir, preencha `CORS_ALLOWED_ORIGINS` com a URL do site na
   Vercel (ex.: `https://compactpay-recarga.vercel.app`, sem barra no fim).
   Se ainda nao tiver, preencha depois em Environment.
3. A chave do painel (`ADMIN_API_KEY`) e' gerada pela Render: veja em
   **Environment**.
4. Teste: `https://<seu-servico>.onrender.com/` responde `{"ok": true}`.

Regras:
- **Uma instancia so.** As conexoes OCPP ficam em memoria.
- O plano **free** dorme sem uso e derruba o WebSocket do carregador. Serve
  para teste; em producao use **starter**. O Postgres free expira em 30 dias.
- O carregador conecta em `wss://<seu-servico>.onrender.com/ocpp/<ID>`. A
  Render so aceita conexao segura (wss): o firmware precisa suportar.

## Diagnostico

`GET /api/v1/admin/diagnostico/servidor` e `GET /api/v1/admin/diagnostico/eventos`
alimentam a tela **Diagnostico** do painel. Elas mostram:
- as tentativas de conexao, inclusive de ID nao cadastrado;
- as conexoes recusadas e o motivo;
- cada mensagem OCPP trocada.

Os eventos ficam em memoria e somem quando o servidor reinicia.
