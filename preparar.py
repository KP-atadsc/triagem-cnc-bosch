"""Extrai as características diretamente do ZIP oficial da Bosch Research."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import urlretrieve

import h5py

from sinais import COLUNAS_BANDAS, FS, assinatura, caracteristicas, janela_central

REV = "d60581d6a3ab6015dcc5488c3d76112bb8e1bcb1"
URL = f"https://codeload.github.com/boschresearch/CNC_Machining/zip/{REV}"
# SHA-256 do inventário ordenado de caminhos e IDs de blobs Git dos 1.702 arquivos HDF5.
INVENTARIO = "e6883cd71420be216275f63cfe5639faa434ed05acd04652a73009a9d97ca63c"
RAIZ = Path(__file__).resolve().parent


def extrair(arquivo: Path, destino: Path, exemplo: Path | None) -> None:
    inventario = []
    assinaturas = set()
    exemplos = []
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destino.parent) as tmp:
        estagio = Path(tmp) / "ciclos.csv.gz"
        with zipfile.ZipFile(arquivo) as z, gzip.open(estagio, "wt", encoding="utf-8", newline="") as f:
            campos = ["id", "assinatura", "maquina", "operacao", "periodo", "rotulo", "origem", "rms", *COLUNAS_BANDAS]
            writer = csv.DictWriter(f, fieldnames=campos)
            writer.writeheader()
            membros = sorted(n for n in z.namelist() if n.endswith(".h5"))
            for nome in membros:
                _, data, maquina, operacao, rotulo, arq = PurePosixPath(nome).parts
                if data != "data" or rotulo not in {"good", "bad"}:
                    raise ValueError(f"Caminho inesperado: {nome}")
                _, mes, ano, _, _ = arq.split("_")
                periodo = ano + "-" + {"Feb": "02", "Aug": "08"}[mes]
                origem = "/".join(PurePosixPath(nome).parts[1:])
                raw = z.read(nome)
                git_id = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
                inventario.append(f"{origem} {git_id}\n")
                x = janela_central(io.BytesIO(raw))
                digest = assinatura(x)
                if digest in assinaturas:
                    raise ValueError(f"Janela de vibração duplicada: {origem}")
                assinaturas.add(digest)
                ciclo = "C-" + digest[:12]
                rms, bandas = caracteristicas(x)
                writer.writerow(dict(id=ciclo, assinatura=digest, maquina=maquina, operacao=operacao,
                                     periodo=periodo, rotulo=rotulo, origem=origem, rms=rms,
                                     **dict(zip(COLUNAS_BANDAS, bandas))))
                if exemplo and periodo == "2021-08":
                    exemplos.append((ciclo, maquina, operacao, nome))
            if hashlib.sha256("".join(inventario).encode()).hexdigest() != INVENTARIO:
                raise ValueError("Os arquivos de vibração diferem da revisão oficial documentada.")
            if exemplo:
                exemplo.mkdir(parents=True, exist_ok=True)
                with (exemplo / "lote.csv").open("w", newline="", encoding="utf-8") as m:
                    manifest = csv.writer(m)
                    manifest.writerow(["ciclo", "maquina", "operacao", "arquivo"])
                    for ciclo, maquina, operacao, nome in sorted(exemplos)[:12]:
                        x = janela_central(io.BytesIO(z.read(nome)))
                        with h5py.File(exemplo / f"{ciclo}.h5", "w") as h:
                            h.attrs["frequencia_hz"] = FS
                            h.create_dataset("vibration_data", data=x, compression="gzip", shuffle=True)
                        manifest.writerow([ciclo, maquina, operacao, f"{ciclo}.h5"])
        estagio.replace(destino)
    print(f"{len(assinaturas)} ciclos verificados e gravados em {destino}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zip", type=Path, help="ZIP da revisão oficial já baixado")
    p.add_argument("--saida", type=Path, default=RAIZ / "dados" / "ciclos.csv.gz")
    p.add_argument("--exemplo", type=Path, help="exportar também 12 janelas HDF5 com manifesto")
    args = p.parse_args()
    if args.zip:
        extrair(args.zip, args.saida, args.exemplo)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "fonte.zip"
            print(f"Baixando a revisão {REV} (cerca de 928 MB)...", flush=True)
            urlretrieve(URL, local)
            extrair(local, args.saida, args.exemplo)
