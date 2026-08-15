import asyncio
from boleto_automator import run_boleto_automation

TEST_ITEM = {
    "emission_id": 127,
    "client_name": "Essência do Cuidar",
    "cnpj_cpf": "53.978.067/0001-61",
    "invoice_number": "3011",
    "boleto_value": 200.0,
    "due_day": 10,
    "bradesco_payer_name": "",
    "payer_cep": "08576-000",
    "payer_endereco": "AVENIDA VEREADOR JOAO FERNANDES DA SILVA",
}

async def progress(evt):
    print(f"[{evt['timestamp']}] {evt['status'].upper()}: {evt['message']}")

async def main():
    await run_boleto_automation([TEST_ITEM], progress_callback=progress)

if __name__ == "__main__":
    asyncio.run(main())
