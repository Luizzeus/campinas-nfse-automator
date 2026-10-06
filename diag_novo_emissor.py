"""Simulação do Novo Emissor (padrão nacional): preenche o assistente até a
revisão final para um cliente e NÃO emite a nota.

Uso: python diag_novo_emissor.py [client_id]   (padrão: 12 - Alisson Araujo)
Salva o DOM de cada etapa em emissao_dom_novo_*.html e screenshots em
screenshots/<MM-AAAA>/.
"""
import asyncio
import sys

from automator import run_nfse_automation


async def main():
    client_id = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    await run_nfse_automation([client_id], dry_run=True, dry_run_pause_seconds=20)


if __name__ == "__main__":
    asyncio.run(main())
