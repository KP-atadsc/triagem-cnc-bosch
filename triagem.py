"""Triagem de vibração CNC. Execute com --help para ver os comandos."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

from sinais import (AMOSTRAS, COLUNAS_BANDAS, EPS, FAIXAS, FS, FIM_TREINO, PERIODOS_TESTE,
                    ajustar, assinatura, caracteristicas, faixa, janela_central, ordenar, pontuar, vagas)

RAIZ = Path(__file__).resolve().parent
NOMES_PERIODOS = {"2021-02": "Fev 2021", "2021-08": "Ago 2021"}
METODOS = {"rms": "z_rms", "espectro": "score"}


def ler_historico(arquivo: Path) -> list[dict]:
    with gzip.open(arquivo, "rt", encoding="utf-8", newline="") as f:
        registros = []
        for linha in csv.DictReader(f):
            bandas = np.array([float(linha[c]) for c in COLUNAS_BANDAS])
            registros.append(dict(id=linha["id"], assinatura=linha["assinatura"], maquina=linha["maquina"],
                                  operacao=linha["operacao"], periodo=linha["periodo"], rotulo=linha["rotulo"],
                                  rms=float(linha["rms"]), bandas=bandas))
    if len({r["assinatura"] for r in registros}) != len(registros):
        raise ValueError("Há janelas duplicadas no histórico.")
    return registros


def validar(registros: list[dict], modelo: dict) -> list[dict]:
    lotes = []
    for periodo in PERIODOS_TESTE:
        ciclos = []
        for r in registros:
            if r["periodo"] != periodo:
                continue
            perfil = modelo["perfis"].get(f"{r['maquina']}/{r['operacao']}")
            if perfil is None:
                raise ValueError(f"Referência ausente no conjunto de teste: {r['maquina']}/{r['operacao']}")
            linha = pontuar(r, perfil)
            linha["anomalo"] = r["rotulo"] == "bad"
            ciclos.append(linha)
        if not ciclos:
            raise ValueError(f"Ausência de ciclos na janela de teste {periodo}.")
        lotes.append(dict(nome=NOMES_PERIODOS[periodo], periodo=periodo, ciclos=ordenar(ciclos), pendencias=[]))
    return lotes


def ler_lote(manifesto: Path, modelo: dict) -> dict:
    ciclos, pendencias = [], []
    ids, hashes = set(), set()
    campos = {"ciclo", "maquina", "operacao", "arquivo"}
    with manifesto.open(encoding="utf-8-sig", newline="") as f:
        leitor = csv.DictReader(f)
        if not campos.issubset(leitor.fieldnames or []):
            raise ValueError("O manifesto precisa das colunas ciclo,maquina,operacao,arquivo.")
        for i, linha in enumerate(leitor, 2):
            ident, m, op, arq = [(linha[c] or "").strip() for c in ("ciclo", "maquina", "operacao", "arquivo")]
            if not ident or ident in ids:
                raise ValueError(f"Linha {i}: identificador de ciclo vazio ou repetido.")
            ids.add(ident)
            try:
                perfil = modelo["perfis"].get(f"{m}/{op}")
                if perfil is None:
                    raise ValueError("combinação de máquina e operação sem referência histórica")
                if not arq:
                    raise ValueError("caminho do arquivo vazio")
                x = janela_central(manifesto.parent / arq)
                digest = assinatura(x)
                if digest in hashes:
                    raise ValueError("janela de sinal repetida neste lote")
                rms, bandas = caracteristicas(x)
                r = dict(id=ident, maquina=m, operacao=op, assinatura=digest, rms=rms, bandas=bandas)
                ciclos.append(pontuar(r, perfil))
                hashes.add(digest)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                pendencias.append(dict(id=ident, maquina=m, operacao=op, motivo=str(exc)))
    if not ids:
        raise ValueError("O manifesto está vazio.")
    return dict(nome=manifesto.stem, periodo=manifesto.stem, ciclos=ordenar(ciclos), pendencias=pendencias)


def medir(lote: dict, cap: int) -> list[dict]:
    ciclos = lote["ciclos"]
    n = len(ciclos)
    k = vagas(n, cap)
    positivos = sum(r["anomalo"] for r in ciclos)
    linhas = []
    for metodo in ("score", "z_rms", "rms", "aleatoria"):
        if metodo == "aleatoria":
            tp = k * positivos / n if n else 0
        else:
            tp = sum(r["anomalo"] for r in ordenar(ciclos, metodo)[:k])
        linhas.append(dict(periodo=lote["periodo"], metodo=metodo, capacidade_pct=cap, ciclos=n,
                           revisados=k, anomalos=positivos, encontrados=tp, normais_na_fila=k - tp,
                           anomalos_fora=positivos - tp, precisao=tp / k if k else "",
                           cobertura=tp / positivos if positivos else ""))
    return linhas


def campo_csv(valor):
    # Ao abrir em planilha, identificadores fornecidos pelo usuário continuam texto.
    if isinstance(valor, str) and valor.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + valor
    return valor


def escrever_csv(arq: Path, colunas: list[str], linhas, delimitador: str = ";") -> None:
    with arq.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=colunas, delimiter=delimitador)
        writer.writeheader()
        for r in linhas:
            writer.writerow({k: campo_csv(v) for k, v in r.items()})


def exportar_filas(arq: Path, lotes: list[dict], cap: int, historico: bool, metodo: str) -> None:
    linhas = []
    colunas = ["lote", "posicao", "selecionado", "ciclo", "maquina", "operacao", "metodo", "pontuacao", "rms_vezes_mediana",
               "eixo", "de_hz", "ate_hz", "vezes_mediana"]
    if historico:
        colunas.append("rotulo_publicado")
    for lote in lotes:
        k = vagas(len(lote["ciclos"]), cap)
        for pos, r in enumerate(ordenar(lote["ciclos"], METODOS[metodo]), 1):
            eixo, de, ate = faixa(r["indice"])
            linha = dict(lote=lote["periodo"], posicao=pos, selecionado="sim" if pos <= k else "não",
                         ciclo=r["id"], maquina=r["maquina"], operacao=r["operacao"],
                         metodo=metodo, pontuacao=r[METODOS[metodo]], rms_vezes_mediana=r["fator_rms"],
                         eixo=eixo, de_hz=de, ate_hz=ate, vezes_mediana=r["fator"])
            if historico:
                linha["rotulo_publicado"] = "bad" if r["anomalo"] else "good"
            linhas.append(linha)
    escrever_csv(arq, colunas, linhas)


def relativo_db(valores, log_mediana) -> list:
    db = 20 / math.log(10) * (np.log(np.maximum(valores, EPS)) - np.asarray(log_mediana))
    return np.round(db, 2).tolist()


def gravar_html(arq: Path, lotes: list[dict], modelo: dict, cap: int, historico: bool, metodo: str) -> None:
    perfis, periodos = {}, []
    for lote in lotes:
        posicoes = {metodo: {r["id"]: i for i, r in enumerate(ordenar(lote["ciclos"], metodo), 1)}
                    for metodo in ("rms", "z_rms")}
        linhas = []
        for r in lote["ciclos"]:
            grupo = f"{r['maquina']}/{r['operacao']}"
            p = modelo["perfis"][grupo]
            if grupo not in perfis:
                perfis[grupo] = dict(n=p["n"], lo=relativo_db(p["p10"], p["centro"]),
                                     hi=relativo_db(p["p90"], p["centro"]))
            reg = dict(id=r["id"], m=r["maquina"], op=r["operacao"], s=round(r["score"], 5),
                       i=r["indice"], f=round(r["fator"], 6), db=relativo_db(r["bandas"], p["centro"]),
                       z=round(r["z_rms"], 5), fr=round(r["fator_rms"], 6), pz=posicoes["z_rms"][r["id"]])
            if historico:
                reg.update(bad=r["anomalo"], pr=posicoes["rms"][r["id"]])
            linhas.append(reg)
        periodos.append(dict(nome=lote["nome"], periodo=lote["periodo"], rows=linhas, pend=lote["pendencias"]))
    data = dict(historico=historico, capacidade=cap, metodo=metodo, lotes=periodos, perfis=perfis,
                nref=sum(p["n"] for p in modelo["perfis"].values()), grupos=len(modelo["perfis"]))
    embutido = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    template = (RAIZ / "interface.html").read_text(encoding="utf-8")
    arq.write_text(template.replace("__DATA_JSON__", embutido), encoding="utf-8")


def executar(args: argparse.Namespace) -> None:
    cap = args.capacidade
    vagas(1, cap)
    historico = args.comando == "validar"
    if historico:
        registros = ler_historico(args.dados)
        modelo = ajustar(registros)
        lotes = validar(registros, modelo)
    else:
        modelo = json.loads(args.modelo.read_text(encoding="utf-8"))
        configuracao = (modelo["versao"], modelo["fs"], modelo["amostras"], modelo["faixas"])
        if configuracao != (1, FS, AMOSTRAS, FAIXAS):
            raise ValueError("Modelo incompatível com esta versão da extração.")
        lotes = [ler_lote(args.manifesto, modelo)]
    args.saida.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.saida) as tmp:
        pasta = Path(tmp)
        if historico:
            (pasta / "referencias.json").write_text(json.dumps(modelo, allow_nan=False), encoding="utf-8")
            medidas = [r for lote in lotes for r in medir(lote, cap)]
            escrever_csv(pasta / "metricas.csv", list(medidas[0]), medidas)
        else:
            escrever_csv(pasta / "pendencias.csv", ["id", "maquina", "operacao", "motivo"], lotes[0]["pendencias"])
        exportar_filas(pasta / "fila.csv", lotes, cap, historico, args.metodo)
        gravar_html(pasta / ("validacao.html" if historico else "fila.html"), lotes, modelo, cap, historico, args.metodo)
        for arq in pasta.iterdir():
            arq.replace(args.saida / arq.name)
    for lote in lotes:
        ciclos = ordenar(lote["ciclos"], METODOS[args.metodo])
        k = vagas(len(ciclos), cap)
        msg = f"{lote['nome']}: {k} para revisão entre {len(ciclos)} ciclos pontuados"
        if historico:
            msg += f", {sum(r['anomalo'] for r in ciclos[:k])}/{sum(r['anomalo'] for r in ciclos)} anômalos encontrados"
        print(msg + f", {len(lote['pendencias'])} pendências.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)
    val = sub.add_parser("validar", help="ajustar a referência de 2019–20 e reavaliar 2021")
    val.add_argument("--dados", type=Path, default=RAIZ / "dados" / "ciclos.csv.gz")
    clf = sub.add_parser("classificar", help="gerar a fila de um lote de arquivos HDF5 sem rótulos")
    clf.add_argument("manifesto", type=Path, help="CSV com ciclo,maquina,operacao,arquivo")
    clf.add_argument("--modelo", type=Path, default=RAIZ / "dados" / "referencias.json")
    for comando in (val, clf):
        comando.add_argument("--saida", type=Path, required=True)
        comando.add_argument("--capacidade", type=int, default=10, help="percentual inteiro de vagas (0–100)")
        comando.add_argument("--metodo", choices=METODOS, default="rms",
                             help="desvio da vibração total (rms) ou por faixa de frequência (espectro)")
    try:
        executar(parser.parse_args())
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        sys.exit(f"Erro: {exc}")
