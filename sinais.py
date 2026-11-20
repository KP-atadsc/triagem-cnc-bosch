"""Assinatura espectral e referência histórica por máquina e operação."""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from typing import BinaryIO

import h5py
import numpy as np

FS = 2000
AMOSTRAS = 8192
FFT = 1024
FAIXAS = 32
EIXOS = "XYZ"
MIN_REFERENCIAS = 8
PISO_LOG = 0.20
EPS = 1e-9
FIM_TREINO = "2020-08"
PERIODOS_TESTE = ("2021-02", "2021-08")
COLUNAS_BANDAS = [f"{e}_b{b:02}" for e in EIXOS for b in range(FAIXAS)]


def janela_central(arquivo: Path | BinaryIO) -> np.ndarray:
    """Lê apenas o trecho necessário. Sem unidade física presumida."""
    with h5py.File(arquivo, "r") as h5:
        if "vibration_data" not in h5:
            raise ValueError("campo vibration_data ausente")
        ds = h5["vibration_data"]
        if not isinstance(ds, h5py.Dataset) or ds.ndim != 2 or ds.shape[1] != 3:
            raise ValueError("esperado vibration_data com três eixos (N × 3)")
        if ds.dtype.kind not in "fiu":
            raise ValueError("as três colunas precisam ser numéricas")
        for attrs in (h5.attrs, ds.attrs):
            if "frequencia_hz" in attrs and float(attrs["frequencia_hz"]) != FS:
                raise ValueError("frequência de amostragem incompatível com 2.000 Hz")
        if len(ds) < AMOSTRAS:
            raise ValueError(f"sinal curto: mínimo de {AMOSTRAS} amostras")
        inicio = (len(ds) - AMOSTRAS) // 2
        x = np.asarray(ds[inicio:inicio + AMOSTRAS], dtype="<f4", order="C")
    if not np.isfinite(x).all():
        raise ValueError("a janela contém valores ausentes ou infinitos")
    if np.any(np.ptp(x.astype(float), axis=0) <= 0):
        raise ValueError("há um eixo sem variação na janela central")
    return x


def assinatura(x: np.ndarray) -> str:
    return hashlib.sha256(x.tobytes(order="C")).hexdigest()


def caracteristicas(x: np.ndarray) -> tuple[float, np.ndarray]:
    """RMS triaxial e RMS de cada banda (32 por eixo) na mesma janela."""
    x = x.astype(np.float64)
    rms = np.sqrt(np.square(x - x.mean(axis=0)).mean(axis=0).sum())
    partes = np.stack([x[i:i + FFT] for i in range(0, AMOSTRAS - FFT + 1, FFT // 2)])
    partes -= partes.mean(axis=1, keepdims=True)
    hann = np.hanning(FFT)
    espectro = np.fft.rfft(partes * hann[None, :, None], axis=1)
    potencia = np.square(np.abs(espectro)).mean(axis=0) / (FFT * np.square(hann).sum())
    potencia[1:-1] *= 2
    # Exclui DC, inclui Nyquist. Cada banda cobre (limite inferior, superior].
    bandas = np.sqrt(potencia[1:].reshape(FAIXAS, -1, 3).sum(axis=1)).T
    return float(rms), bandas.reshape(-1)


def estatisticas(valores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    log = np.log(np.maximum(valores, EPS))
    centro = np.median(log, axis=0)
    escala = np.maximum(PISO_LOG, 1.4826 * np.median(np.abs(log - centro), axis=0))
    return centro, escala


def ajustar(linhas: list[dict]) -> dict:
    grupos = defaultdict(list)
    for r in linhas:
        if r["periodo"] <= FIM_TREINO and r["rotulo"] == "good":
            grupos[f"{r['maquina']}/{r['operacao']}"].append(r)
    perfis = {}
    for grupo, registros in sorted(grupos.items()):
        if len(registros) < MIN_REFERENCIAS:
            continue
        a = np.stack([r["bandas"] for r in registros])
        centro, escala = estatisticas(a)
        c_rms, e_rms = estatisticas(np.array([r["rms"] for r in registros]))
        p10, p90 = np.quantile(a, [0.1, 0.9], axis=0)
        perfis[grupo] = dict(n=len(a), centro=centro.tolist(), escala=escala.tolist(),
                             p10=p10.tolist(), p90=p90.tolist(),
                             centro_rms=float(c_rms), escala_rms=float(e_rms))
    return {"versao": 1, "periodo_final": FIM_TREINO, "fs": FS,
            "amostras": AMOSTRAS, "faixas": FAIXAS, "perfis": perfis}


def pontuar(r: dict, perfil: dict) -> dict:
    log = np.log(np.maximum(r["bandas"], EPS))
    delta = log - np.asarray(perfil["centro"])
    z = np.abs(delta) / np.asarray(perfil["escala"])
    indices = np.argsort(-z, kind="stable")[:4]
    principal = int(indices[0])
    referencia = math.exp(perfil["centro"][principal])
    return dict(id=r["id"], maquina=r["maquina"], operacao=r["operacao"],
                assinatura=r["assinatura"], score=float(np.mean(z[indices])),
                rms=float(r["rms"]), fator_rms=r["rms"] / math.exp(perfil["centro_rms"]),
                z_rms=abs(math.log(max(r["rms"], EPS)) - perfil["centro_rms"]) / perfil["escala_rms"],
                indice=principal, fator=float(r["bandas"][principal]) / referencia,
                bandas=np.asarray(r["bandas"]).tolist())


def ordenar(ciclos: list[dict], metodo: str = "score") -> list[dict]:
    return sorted(ciclos, key=lambda r: (-r[metodo], r["assinatura"], r["id"]))


def vagas(n: int, percentual: int) -> int:
    if not 0 <= percentual <= 100:
        raise ValueError("a capacidade deve estar entre 0 e 100")
    return n * percentual // 100


def faixa(indice: int) -> tuple[str, float, float]:
    eixo, banda = divmod(indice, FAIXAS)
    largura = FS / 2 / FAIXAS
    return EIXOS[eixo], banda * largura, (banda + 1) * largura
