#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generar_dron.py (versión 2): animación en bucle de un dron submarino (80C552) renderizada por
RAYTRACING por software con numpy (vectorizado) y Pillow. Sin GPU ni OpenGL.

El bucle dura 40 s y encadena tres recorridos en circuito cerrado (el último fotograma enlaza
con el primero sin fundidos):
  1. Completo:    C2 A5 D2 A3 E2 A4 F0
  2. Obstáculos:  C3 E4 A6 D1 A8 F0   se ignora un obstáculo durante el giro E4 y otro aborta el avance A8
  3. Paro:        C1 D5 A4 F0         se pulsa Marcha/Paro durante el giro D5 y el dron sube

Uso:
    python generar_dron.py                     todo: comprueba, renderiza los fotogramas y monta los archivos
    python generar_dron.py --escala 0.25       lo mismo a 800x450 (prueba rápida)
    python generar_dron.py --fotograma 120 --salida prueba.png
    python generar_dron.py --tabla             solo la tabla de tiempos
    python generar_dron.py --comprobar         solo las comprobaciones
    python generar_dron.py --render            solo los fotogramas PNG (se puede interrumpir y reanudar)
    python generar_dron.py --montar            solo monta WebP/MP4 a partir de los fotogramas
Opciones: --dir CARPETA, --salida BASE, --procesos N, --desde A, --hasta B, --paso K,
          --mb4k / --mb / --mbmp4 (peso máximo en MB de cada archivo), --ligera F (tamaño de la versión ligera).
Salidas: BASE_4k.webp (3200x1800), BASE.webp (960x540, para el README), BASE_4k.mp4 y BASE_tiempos.md.

Requisitos: Python 3.9+, numpy y Pillow con WebP. ffmpeg es opcional (solo para el MP4).

Raytracer: un rayo por píxel con intersección analítica (planos, elipsoides y cajas en su sistema
local), sol direccional con sombras duras, luz ambiental, foco en el morro, UN rebote de reflexión
(metal del dron, burbujas y superficie vista desde abajo), atenuación de Beer-Lambert, cáusticas
procedurales y rayos de luz volumétricos. Aceleración: rectángulo de pantalla por primitiva para
los rayos primarios y descarte por teselas para sombras y reflexiones.
"""
import io
import math
import os
import shutil
import subprocess
import sys
import time
from multiprocessing import Pool, cpu_count

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

f32 = np.float32


def _arg(nombre, defecto, tipo=str):
    if nombre in sys.argv:
        return tipo(sys.argv[sys.argv.index(nombre) + 1])
    return defecto


# ----------------------------------------------------------------------------------
# PARÁMETROS
# ----------------------------------------------------------------------------------
ESCALA_RES = _arg("--escala", 1.0, float)        # 1.0 = 3200x1800 (casi 4K)
W = max(2, int(round(3200 * ESCALA_RES / 2)) * 2)
H = max(2, int(round(1800 * ESCALA_RES / 2)) * 2)
FPS = 15
T_ANIM = 40.0             # duración del bucle (s de animación)
ESCALA_T = 1.4            # s de sistema por cada s de animación (56 s de sistema en 40 s)
T_SIS = T_ANIM * ESCALA_T
NF = int(round(T_ANIM * FPS))
SEMILLA = 552             # semilla fija: resultado reproducible
TESELA = 200              # lado de las teselas de sombreado (píxeles)

# Parámetros de animación (NO vienen en el enunciado)
V_AVANCE = 0.5            # m/s en la dirección del rumbo
V_GIRO = 45.0             # grados/s sobre el eje vertical
T_VERTICAL = 3.0          # s de cada descenso o ascenso, con interpolación suave
M_POR_NIVEL = 2.5         # C<n>: profundidad = 2,5 m x n (esto sí es del enunciado)
UMBRAL_OBST = 0.2         # m: obstáculo al frente que aborta el avance (enunciado)
REV_POR_S = 2.0           # hélices: vueltas por s de animación con PWM al 100 %
INICIO = np.array([-2.25, 0.75])                 # posición inicial del dron (x, y); rumbo 0 = +X

# Los tres recorridos. evento = (tipo, índice de la instrucción que se corta, s transcurridos)
RECORRIDOS = [
    dict(nombre="1 Completo", programa=["C2", "A5", "D2", "A3", "E2", "A4", "F0"], espera=0.5, evento=None),
    dict(nombre="2 Obstáculos", programa=["C3", "E4", "A6", "D1", "A8", "F0"], espera=1.0,
         evento=("obstaculo", 4, 1.5 * math.sqrt(2) / V_AVANCE),
         notas={1: "obstáculo a menos de 0,2 m durante el giro: se ignora",
                4: "obstáculo a menos de 0,2 m durante el avance: aborta"}),
    dict(nombre="3 Paro", programa=["C1", "D5", "A4", "F0"], espera=1.0, evento=("paro", 1, 3.0),
         notas={1: "se pulsa Marcha/Paro durante el giro"}),
]

# Escena (metros). Superficie en z = 0, fondo en z = -10,5, volumen de 12 x 12 m
Z_FONDO = -10.5
POSTE = np.array([0.6, 2.3])                     # poste de escala de profundidad
MARCAS = [0.0, 2.5, 5.0, 7.5, 10.0]
ARRECIFE = np.array([-2.5, 4.6])                 # jardín de coral (sin algas altas)
RESPIRADEROS = [(-0.6, 2.2), (3.6, -2.4), (-4.4, -1.2)]   # columnas de burbujas

# Luz y agua
SOL = np.array([0.15, -0.50, 1.0]); SOL = SOL / np.linalg.norm(SOL)   # dirección hacia el sol
KX, KY = SOL[0] / SOL[2], SOL[1] / SOL[2]        # proyección a lo largo del sol sobre z = 0
SOL_COL = np.array([1.00, 0.96, 0.86], f32) * 2.9
AMB_COL = np.array([0.30, 0.50, 0.58], f32) * 0.62
SIGMA = np.array([0.110, 0.046, 0.028], f32)     # extinción Beer-Lambert por canal (1/m)
AGUA_SOMERA = np.array([0.030, 0.33, 0.52], f32)
AGUA_HONDA = np.array([0.0, 0.025, 0.085], f32)
CIELO = np.array([0.80, 0.95, 1.00], f32) * 1.3
AGUA_REFLEJO = AGUA_SOMERA * 0.6
FOCO_COL = np.array([1.00, 0.95, 0.80], f32) * 5.0
FOCO_INCL = 12.0                                 # grados que apunta hacia abajo
FOCO_COS_INT, FOCO_COS_EXT = 0.95, 0.78          # cono de iluminación del foco
HAZ_COS_INT, HAZ_COS_EXT = 0.975, 0.90           # cono del haz visible en el agua
HAZ_COL = np.array([1.00, 0.96, 0.82], f32) * 0.38
RAYOS_COL = np.array([0.55, 0.95, 1.00], f32) * 0.11
EXPOSICION = 1.15
EPS, DESP = 1e-4, 2e-3

# Cámara
CAM_DIST = 5.4            # distancia horizontal al objetivo
CAM_AZ0 = -105.0          # acimut base (grados): vista en 3/4
CAM_ORBITA = 15.0         # órbita de +-15 grados en todo el bucle
CAM_HFOV = 60.0
FOCAL = (W / 2) / math.tan(math.radians(CAM_HFOV / 2))
XS = (np.arange(W) - W / 2 + 0.5) / FOCAL        # dirección del rayo de cada columna y fila
YS = (H / 2 - np.arange(H) - 0.5) / FOCAL


def suave(u):
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3 - 2 * u)


# ----------------------------------------------------------------------------------
# FASES: única fuente de tiempos
# ----------------------------------------------------------------------------------
def construir_fases():
    fases, botones = [], []
    st = dict(t=0.0, rumbo=0.0, prof=0.0, pos=INICIO.astype(float))

    def nueva(rec, nombre, ins, letra, chip, dur, tipo, rumbo1, prof1, pos1, pwm0, pwm1, ga, sb, param=0, nota=""):
        fases.append(dict(n=len(fases), rec=rec, nombre=nombre, ins=ins, letra=letra, chip=chip,
                          t0=st["t"], t1=st["t"] + dur, tipo=tipo, rumbo0=st["rumbo"], rumbo1=rumbo1,
                          prof0=st["prof"], prof1=prof1, pos0=st["pos"].copy(), pos1=np.array(pos1, float),
                          pwm0=pwm0, pwm1=pwm1, ga=ga, sb=sb, param=param, nota=nota))
        st.update(t=st["t"] + dur, rumbo=rumbo1, prof=prof1, pos=np.array(pos1, float))

    for r, rec in enumerate(RECORRIDOS):
        nueva(r, "Espera" if r == 0 else "Reposo", "", None, None, rec["espera"], "reposo",
              st["rumbo"], st["prof"], st["pos"], 0, 0, "00", 0)
        botones.append(st["t"])                                    # se pulsa Marcha
        prog, ev = rec["programa"], rec["evento"]
        for k, ins in enumerate(prog):
            a, p = ins[0], int(ins[1], 16)
            corte = ev[2] if ev and ev[1] == k else None
            nota = rec.get("notas", {}).get(k, "")
            if a == "C":
                p1 = M_POR_NIVEL * p
                nueva(r, "Descenso" if p1 > st["prof"] else "Ascenso", ins, "C", k, T_VERTICAL, "vertical",
                      st["rumbo"], p1, st["pos"], 70, 0, "00", 1 if p1 < st["prof"] else 0, p, nota)
            elif a == "A":
                dur = float(p) if corte is None else corte
                rr = math.radians(st["rumbo"])
                d = np.array([math.cos(rr), -math.sin(rr)])        # rumbo 0 = +X; horario = positivo
                nueva(r, "Avance", ins, "A", k, dur, "avance", st["rumbo"], st["prof"],
                      st["pos"] + V_AVANCE * dur * d, 30, 50, "11", 0, p, nota)
            elif a in "DE":
                dur = float(p) if corte is None else corte
                sg = 1.0 if a == "D" else -1.0
                nueva(r, "Giro dcha" if a == "D" else "Giro izda", ins, "d" if a == "D" else "E", k, dur, "giro",
                      st["rumbo"] + sg * V_GIRO * dur, st["prof"], st["pos"], 30, 50,
                      "10" if a == "D" else "01", 0, p, nota)
            elif a == "F":
                nueva(r, "Ascenso", ins, "F", k, T_VERTICAL, "vertical", st["rumbo"], 0.0, st["pos"],
                      70, 0, "00", 1, p, nota)
            if corte is not None:                                  # el recorrido se corta: sube
                if ev[0] == "paro":
                    botones.append(st["t"])                        # se pulsa Paro
                nueva(r, "Ascenso", "", "F", len(prog) - 1, T_VERTICAL, "vertical", st["rumbo"], 0.0, st["pos"],
                      70, 0, "00", 1, 0, "aborto por obstáculo" if ev[0] == "obstaculo" else "paro con el botón")
                break
    assert st["t"] < T_SIS - 0.9, "Los recorridos no caben en el bucle"
    nueva(0, "Reposo", "", None, None, T_SIS - st["t"], "reposo", st["rumbo"], st["prof"], st["pos"], 0, 0, "00", 0)
    for a, b in zip(fases, fases[1:]):
        assert abs(a["t1"] - b["t0"]) < 1e-9                       # encadenado exacto
    # circuito cerrado: el bucle termina en la pose inicial
    assert np.allclose(st["pos"], INICIO, atol=1e-9) and abs(st["rumbo"]) % 360 < 1e-9 and st["prof"] == 0
    return fases, botones


FASES, BOTONES = construir_fases()
_T0S = np.array([f["t0"] for f in FASES])


def z_dron(prof):
    """A 0 m el dron queda 0,2 m bajo la superficie para verlo desde abajo; desde 2,5 m es exacto."""
    return -(prof + 0.2 * max(0.0, 1.0 - prof / 2.5))


def estado(ts):
    """Estado del sistema en el instante ts (s de sistema)."""
    ts = ts % T_SIS
    f = FASES[int(np.searchsorted(_T0S, ts, side="right")) - 1]
    u = (ts - f["t0"]) / (f["t1"] - f["t0"])
    e = dict(fase=f, u=u, ts=ts)
    e["xy"] = f["pos0"] + (f["pos1"] - f["pos0"]) * u
    e["prof"] = f["prof0"] + (f["prof1"] - f["prof0"]) * (float(suave(u)) if f["tipo"] == "vertical" else u)
    e["rumbo"] = f["rumbo0"] + (f["rumbo1"] - f["rumbo0"]) * u
    if f["tipo"] == "reposo":
        e["d1"], e["d2"], e["dp"] = None, None, False
    elif f["tipo"] == "vertical":                      # sin tiempo: muestra el parámetro; dp encendido
        e["d1"], e["d2"], e["dp"] = f["letra"], "%X" % f["param"], True
    else:                                              # segundos restantes de la instrucción, en hexadecimal
        e["d1"], e["d2"], e["dp"] = f["letra"], "%X" % max(1, math.ceil(f["param"] - (ts - f["t0"]) - 1e-9)), False
    e["boton"] = any(-0.30 <= ts - tb <= 0.16 for tb in BOTONES)
    return e


def tabla_tiempos():
    def n(x, d=1):
        return (("%." + str(d) + "f") % x).replace(".", ",")

    def rango(a, b, u, d=0):
        return n(a, d) + u if abs(a - b) < 1e-9 else n(a, d) + " → " + n(b, d) + u

    L = ["| Recorrido | Fase | Inicio (sist.) | Fin (sist.) | Inicio (anim.) | Fin (anim.) | Instrucción | Rumbo | Profundidad | PWM0 | PWM1 | Giro_Avance | Subir_Bajar | Evento |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for f in FASES:
        L.append("| %s | %d %s | %s s | %s s | %s s | %s s | %s | %s | %s | %d %% | %d %% | %s | %d | %s |" % (
            RECORRIDOS[f["rec"]]["nombre"], f["n"], f["nombre"], n(f["t0"], 2), n(f["t1"], 2),
            n(f["t0"] / ESCALA_T, 2), n(f["t1"] / ESCALA_T, 2), f["ins"] or "ninguna",
            rango(f["rumbo0"], f["rumbo1"], "°"), rango(f["prof0"], f["prof1"], " m", 1),
            f["pwm0"], f["pwm1"], f["ga"], f["sb"], f["nota"]))
    L += ["",
          "Bucle de %s s de animación = %s s de sistema (1 s de animación = %s s de sistema), %d fotogramas a %d fps."
          % (n(T_ANIM, 0), n(T_SIS, 0), n(ESCALA_T), NF, FPS),
          "Rumbo: 0° = dirección inicial (+X); positivo = giro horario visto desde arriba.",
          "Marcha/Paro se pulsa en los instantes (s de sistema): " + ", ".join(n(t, 2) for t in BOTONES) + ".",
          "Parámetros de animación (NO vienen en el enunciado): avance %s m/s, giro %s °/s, descenso y ascenso de %s s "
          "con interpolación suave, esperas entre recorridos y escala de tiempo x%s. Con el paro se supone que el dron sube."
          % (n(V_AVANCE), n(V_GIRO, 0), n(T_VERTICAL, 0), n(ESCALA_T))]
    return "\n".join(L)


# ----------------------------------------------------------------------------------
# ESTADO POR FOTOGRAMA (posición, hélices, cámara)
# ----------------------------------------------------------------------------------
EST = [estado(i / FPS * ESCALA_T) for i in range(NF)]
POS = np.array([[e["xy"][0], e["xy"][1], z_dron(e["prof"])] for e in EST])


def _angulos(vel):
    """Ángulo acumulado de una hélice; se ajusta para cerrar el bucle en un múltiplo de 180 grados."""
    ang = np.cumsum(vel) * 2 * math.pi * REV_POR_S / FPS
    if abs(ang[-1]) > 1e-9:
        ang *= round(ang[-1] / math.pi) * math.pi / ang[-1]
    return ang


_SIG = {"11": (1, 1), "10": (1, -1), "01": (-1, 1), "00": (0, 0)}
ANG_V = _angulos([e["fase"]["pwm0"] / 100 for e in EST])
ANG_RI = _angulos([_SIG[e["fase"]["ga"]][0] * e["fase"]["pwm1"] / 100 for e in EST])
ANG_RD = _angulos([_SIG[e["fase"]["ga"]][1] * e["fase"]["pwm1"] / 100 for e in EST])


def _objetivos():
    """Objetivo de cámara: posición del dron suavizada con una ventana circular (periódica)."""
    nv = 29
    w = np.hanning(nv + 2)[1:-1]; w /= w.sum()
    return sum(w[k] * np.roll(POS, -(k - nv // 2), axis=0) for k in range(nv))


OBJ = _objetivos()


def camara(i):
    t = i / FPS
    az = math.radians(CAM_AZ0 + CAM_ORBITA * math.sin(2 * math.pi * t / T_ANIM))
    o = OBJ[i]
    pos = np.array([o[0] + CAM_DIST * math.cos(az), o[1] + CAM_DIST * math.sin(az), -0.45 + 0.55 * o[2]])
    f = o + np.array([0, 0, -0.25]) - pos; f /= np.linalg.norm(f)
    r = np.cross(f, [0, 0, 1.0]); r /= np.linalg.norm(r)
    u = np.cross(r, f)
    return pos, f, r, u


def proyectar(cam, P):
    pos, f, r, u = cam
    v = np.asarray(P, float) - pos
    zc = v @ f
    zs = np.where(np.abs(zc) < 1e-6, 1e-6, zc)
    return W / 2 + FOCAL * (v @ r) / zs, H / 2 - FOCAL * (v @ u) / zs, zc


# ----------------------------------------------------------------------------------
# GEOMETRÍA: elipsoides y cajas definidos por centro, rotación R y semiejes
# ----------------------------------------------------------------------------------
ELI, CAJA = 0, 1
I3 = np.eye(3)
_SIG8 = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], float)


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0, 0], [0, c, -s], [0, s, c]])


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1.0, 0], [-s, 0, c]])


def base_eje(ex, giro=0.0):
    """Base ortonormal cuyo primer eje es ex (el segundo, horizontal salvo giro)."""
    ex = np.asarray(ex, float); ex = ex / np.linalg.norm(ex)
    ey = np.cross([0, 0, 1.0], ex); nn = np.linalg.norm(ey)
    ey = np.array([1.0, 0, 0]) if nn < 1e-6 else ey / nn
    if giro:
        ey = ey * math.cos(giro) + np.cross(ex, ey) * math.sin(giro)
    return np.column_stack([ex, ey, np.cross(ex, ey)])


class Lista:
    """Lista de primitivas agrupadas; cada grupo tiene un solo tipo y una caja envolvente."""

    def __init__(self):
        self.C, self.A, self.tipo, self.alb, self.mat, self.grupos = [], [], [], [], [], []
        self._g0 = 0

    def add(self, tipo, c, R, semi, alb, refl=0.0, emi=(0.0, 0.0, 0.0), esp=0.0, trans=0.0, patron=0, solido=1):
        self.C.append(np.asarray(c, float)); self.A.append(np.asarray(R, float) * np.asarray(semi, float))
        self.tipo.append(tipo); self.alb.append(alb)
        self.mat.append((refl, emi[0], emi[1], emi[2], esp, trans, patron, solido))

    def cerrar_grupo(self):
        if len(self.C) > self._g0:
            self.grupos.append((self._g0, len(self.C)))
        self._g0 = len(self.C)

    def matrices(self):
        self.cerrar_grupo()
        if not self.C:
            return None
        return dict(C=np.array(self.C), A=np.array(self.A), tipo=np.array(self.tipo),
                    alb=np.array(self.alb, f32), mat=np.array(self.mat, float), grupos=list(self.grupos))


def tramo(l, p, q, ry, rz, alb, giro=0.0, solape=0.58, **mat):
    """Elipsoide alargado entre dos puntos."""
    d = q - p
    l.add(ELI, (p + q) / 2, base_eje(d, giro), [solape * np.linalg.norm(d), ry, rz], alb, **mat)


class Escena:
    def __init__(self, partes, foco_pos, foco_dir, t):
        C = np.concatenate([p["C"] for p in partes]); A = np.concatenate([p["A"] for p in partes])
        mat = np.concatenate([p["mat"] for p in partes])
        self.C = C; self.M = np.linalg.inv(A)                      # mundo -> local unitario
        self.tipo = np.concatenate([p["tipo"] for p in partes])
        self.alb = np.concatenate([p["alb"] for p in partes])
        self.refl = mat[:, 0].astype(f32); self.emi = mat[:, 1:4].astype(f32); self.esp = mat[:, 4].astype(f32)
        self.trans = mat[:, 5].astype(f32); self.patron = mat[:, 6].astype(np.int8); solido = mat[:, 7] > 0
        eli = self.tipo == ELI
        self.esq = C[:, None, :] + np.einsum("kij,ej->kei", A, _SIG8)          # esquinas de la caja orientada
        ext = np.where(eli[:, None], np.sqrt((A * A).sum(2)), np.abs(A).sum(2))
        self.plo = C - ext; self.phi = C + ext

        def funcional(w):                                          # extensión exacta a lo largo de w
            Aw = np.einsum("kji,j->ki", A, w)
            e = np.where(eli, np.sqrt((Aw * Aw).sum(1)), np.abs(Aw).sum(1)); c = C @ w
            return c - e, c + e
        self.pu0, self.pu1 = funcional(np.array([1.0, 0.0, -KX]))  # "espacio del sol": (u, v) = proyección
        self.pv0, self.pv1 = funcional(np.array([0.0, 1.0, -KY]))  # a lo largo del sol sobre z = 0
        self.ML = self.M @ SOL; self.qaL = (self.ML * self.ML).sum(1)
        ga, gb, base = [], [], 0
        for p in partes:
            for a, b in p["grupos"]:
                ga.append(a + base); gb.append(b + base)
            base += len(p["C"])
        self.ga = np.array(ga); self.gb = np.array(gb)
        assert self.ga[0] == 0 and self.gb[-1] == len(C) and (self.gb[:-1] == self.ga[1:]).all()
        self.gtipo = self.tipo[self.ga]; self.gsol = solido[self.ga]
        self.glo = np.minimum.reduceat(self.plo, self.ga, axis=0) - 1e-3
        self.ghi = np.maximum.reduceat(self.phi, self.ga, axis=0) + 1e-3
        self.gu0 = np.minimum.reduceat(self.pu0, self.ga); self.gu1 = np.maximum.reduceat(self.pu1, self.ga)
        self.gv0 = np.minimum.reduceat(self.pv0, self.ga); self.gv1 = np.maximum.reduceat(self.pv1, self.ga)
        self.gz1 = self.ghi[:, 2]
        self.gesq = self.glo[:, None, :] + (self.ghi - self.glo)[:, None, :] * (_SIG8[None] + 1) / 2
        self.lo = self.glo.min(0); self.hi = self.ghi.max(0)
        self.foco_pos, self.foco_dir, self.t = foco_pos, foco_dir, t
        self.foco_rango = 5.0
        # Proyección desde el foco sobre el plano perpendicular a su eje: sirve para saber qué primitivas
        # pueden tapar cada punto iluminado (igual que el "espacio del sol", pero en perspectiva).
        e1 = np.cross(foco_dir, [0, 0, 1.0]); e1 /= np.linalg.norm(e1); e2 = np.cross(foco_dir, e1)
        self.fe1, self.fe2 = e1, e2
        v = self.esq - foco_pos
        w = v @ foco_dir
        dl = w > 0.05
        ws = np.where(dl, w, 1.0)
        a = (v @ e1) / ws; b = (v @ e2) / ws
        todos = dl.all(1); alguno = dl.any(1); G = 1e9
        self.fa0 = np.where(todos, a.min(1), np.where(alguno, -G, G)); self.fa1 = np.where(todos, a.max(1), np.where(alguno, G, -G))
        self.fb0 = np.where(todos, b.min(1), np.where(alguno, -G, G)); self.fb1 = np.where(todos, b.max(1), np.where(alguno, G, -G))
        self.fw0 = np.where(todos, w.min(1), np.where(alguno, 0.0, G))
        self.gfa0 = np.minimum.reduceat(self.fa0, self.ga); self.gfa1 = np.maximum.reduceat(self.fa1, self.ga)
        self.gfb0 = np.minimum.reduceat(self.fb0, self.ga); self.gfb1 = np.maximum.reduceat(self.fb1, self.ga)
        self.gfw0 = np.minimum.reduceat(self.fw0, self.ga)


def _rayo_prims(C, M, tipo, o, d):
    """Un rayo contra todas las primitivas: distancia a cada una (inf si no corta)."""
    ol = np.einsum("kij,kj->ki", M, o - C); dl = M @ d
    qa = (dl * dl).sum(1); qb = (ol * dl).sum(1); qc = (ol * ol).sum(1) - 1.0
    disc = qb * qb - qa * qc
    te = np.where(disc > 0, (-qb - np.sqrt(np.maximum(disc, 0.0))) / qa, np.inf)
    te[te < EPS] = np.inf
    inv = 1.0 / np.where(np.abs(dl) < 1e-12, 1e-12, dl)
    t0 = (-1.0 - ol) * inv; t1 = (1.0 - ol) * inv
    tn = np.minimum(t0, t1).max(1); tf = np.maximum(t0, t1).min(1)
    tc = np.where((tf >= tn) & (tn > EPS), tn, np.inf)
    return np.where(tipo == ELI, te, tc)


def apoyo(l, x, y):
    """Altura z de lo primero que encuentra un rayo vertical: para posar objetos sobre rocas o arena."""
    if not l.C:
        return Z_FONDO
    t = _rayo_prims(np.array(l.C), np.linalg.inv(np.array(l.A)), np.array(l.tipo),
                    np.array([x, y, -0.3]), np.array([0, 0, -1.0])).min()
    return max(-0.3 - t, Z_FONDO) if np.isfinite(t) else Z_FONDO


# ----------------------------------------------------------------------------------
# ELEMENTOS FIJOS: rocas, obstáculos de los recorridos, corales y poste
# ----------------------------------------------------------------------------------
RNG = np.random.default_rng(SEMILLA)
E1 = INICIO + np.array([4.5, -1.5])                                # donde termina el recorrido 1
DIR_ABORTO = np.array([-math.sqrt(0.5), math.sqrt(0.5)])           # rumbo del avance que aborta (-135 grados)
RUTA2 = np.array([E1, E1 + [-3.0, 0.0], INICIO])                   # recorrido 2, a 7,5 m: pasa cerca del fondo
CORALES = [(0.95, 0.35, 0.30), (1.00, 0.55, 0.20), (0.95, 0.40, 0.65), (0.75, 0.42, 0.92),
           (0.98, 0.80, 0.30), (0.35, 0.80, 0.75), (0.95, 0.92, 0.85)]


def gris(g):
    return np.array([g * 1.12, g, g * 0.86])


def _dist_polilinea(p, pts):
    d = 1e9
    for a, b in zip(pts, pts[1:]):
        ab = b - a
        u = np.clip((p - a) @ ab / (ab @ ab), 0, 1)
        d = min(d, float(np.linalg.norm(p - a - u * ab)))
    return d


def roca(l, xy, tam, rng, n=5):
    for k in range(n):
        off = rng.normal(0, 0.35 * tam, 2) if k else np.zeros(2)
        s = tam * rng.uniform(0.55, 0.95) if k else tam
        semi = [s * rng.uniform(0.9, 1.4), s * rng.uniform(0.8, 1.2), s * rng.uniform(0.45, 0.75)]
        l.add(ELI, [xy[0] + off[0], xy[1] + off[1], Z_FONDO + 0.3 * semi[2]], rot_z(rng.uniform(0, math.pi)), semi,
              gris(rng.uniform(0.30, 0.46)), patron=2)
    l.cerrar_grupo()


def coral_ramas(l, base, alto, col, rng):
    col = np.array(col)

    def rama(p, d, lon, r, nivel):
        q = p + d * lon
        l.add(ELI, (p + q) / 2, base_eje(d), [0.56 * lon, r, r], col * (0.82 + 0.12 * nivel), emi=tuple(col * 0.07))
        if nivel < 2:
            for _ in range(3 if nivel == 0 else 2):
                ang = rng.uniform(0, 2 * math.pi)
                d2 = d + np.array([math.cos(ang), math.sin(ang), 0]) * rng.uniform(0.55, 1.0)
                d2[2] = max(d2[2], 0.25); d2 /= np.linalg.norm(d2)
                rama(q - d * 0.12 * lon, d2, lon * rng.uniform(0.55, 0.78), r * 0.72, nivel + 1)
    rama(np.array(base, float), np.array([0, 0, 1.0]), alto * 0.42, alto * 0.07, 0)
    l.cerrar_grupo()


def coral_cerebro(l, base, r, col, rng):
    l.add(ELI, [base[0], base[1], base[2] + 0.30 * r], rot_z(rng.uniform(0, 3.1)), [r, r * 0.9, r * 0.72], col, patron=3,
          emi=tuple(np.array(col) * 0.05))
    l.cerrar_grupo()


def esponjas(l, base, alto, col, rng):
    for _ in range(int(rng.integers(3, 6))):
        h = alto * rng.uniform(0.6, 1.0); off = rng.normal(0, 0.09, 2)
        R = rot_x(rng.uniform(-0.2, 0.2)) @ rot_y(rng.uniform(-0.2, 0.2))
        l.add(ELI, [base[0] + off[0], base[1] + off[1], base[2] + 0.42 * h], R, [0.065, 0.065, 0.5 * h],
              np.array(col) * rng.uniform(0.85, 1.05), emi=tuple(np.array(col) * 0.06))
    l.cerrar_grupo()


def abanico(l, base, alto, col, rng):
    a = rng.uniform(0, math.pi)
    l.add(ELI, [base[0], base[1], base[2] + 0.12 * alto], I3, [0.03, 0.03, 0.14 * alto], np.array(col) * 0.6)
    l.add(ELI, [base[0], base[1], base[2] + 0.60 * alto], rot_z(a), [0.42 * alto, 0.014, 0.42 * alto], col,
          trans=0.45, patron=3, emi=tuple(np.array(col) * 0.06))
    l.cerrar_grupo()


def anemona(l, base, r, col, rng):
    b = np.array(base, float)
    l.add(ELI, b + [0, 0, 0.5 * r], I3, [r, r, 0.6 * r], np.array(col) * 0.55)
    for k in range(12):
        ang = 2 * math.pi * k / 12 + rng.uniform(-0.2, 0.2); inc = rng.uniform(0.25, 0.9)
        d = np.array([math.cos(ang) * math.sin(inc), math.sin(ang) * math.sin(inc), math.cos(inc)])
        p = b + [0, 0, 0.9 * r] + d * 0.3 * r
        tramo(l, p, p + d * r * rng.uniform(1.3, 1.9), 0.16 * r, 0.16 * r, col, trans=0.5, emi=tuple(np.array(col) * 0.08))
    l.cerrar_grupo()


def estrella(l, base, r, col, rng):
    a0 = rng.uniform(0, 1.2); b = np.array(base, float) + [0, 0, 0.012]
    for k in range(5):
        a = a0 + 2 * math.pi * k / 5
        l.add(ELI, b + r * 0.55 * np.array([math.cos(a), math.sin(a), 0]), rot_z(a), [0.62 * r, 0.20 * r, 0.11 * r], col,
              emi=tuple(np.array(col) * 0.06))
    l.add(ELI, b, I3, [0.28 * r, 0.28 * r, 0.13 * r], col, emi=tuple(np.array(col) * 0.06))
    l.cerrar_grupo()


def _t_eli(c, A, o, d):
    M = np.linalg.inv(A); ol = M @ (o - c); dl = M @ d
    qa = dl @ dl; qb = ol @ dl; disc = qb * qb - qa * (ol @ ol - 1.0)
    return (-qb - math.sqrt(disc)) / qa


def _fijos():
    rng = RNG; l = Lista()
    ocup = [(POSTE[0], POSTE[1], 0.5)] + [(x, y, 0.45) for x, y in RESPIRADEROS]
    z75 = z_dron(7.5)
    # Roca que hace abortar el recorrido 2. Se coloca para que, al llegar el dron a INICIO, el sónar del
    # morro mida justo por debajo del umbral de 0,2 m.
    semi = np.array([1.25, 1.05, 2.3]); R = rot_z(math.radians(155)); d3 = np.array([DIR_ABORTO[0], DIR_ABORTO[1], 0.0])
    nariz = np.array([INICIO[0], INICIO[1], z75]) + 0.335 * d3
    c = np.array([INICIO[0], INICIO[1], Z_FONDO + 1.9]) + 2.0 * d3
    c = c + d3 * ((UMBRAL_OBST - 0.006) - _t_eli(c, R * semi, nariz, d3))
    lat = np.array([-d3[1], d3[0], 0.0])
    l.add(ELI, c, R, semi, gris(0.40), patron=2)
    for a, b, h, s in ((0.9, 0.9, -0.9, 0.9), (1.2, -0.8, -1.2, 0.8), (0.3, -1.35, -1.5, 0.7), (1.9, 0.1, -1.6, 0.75),
                       (0.5, 0.5, 0.9, 0.6), (0.2, -0.35, 1.6, 0.42), (-0.9, 1.1, -1.55, 0.6), (-1.2, -0.5, -1.6, 0.55),
                       (-0.6, -1.3, -1.6, 0.5), (-1.5, 0.4, -1.65, 0.45), (0.45, 1.05, 0.1, 0.62), (0.7, -1.0, -0.2, 0.66),
                       (1.3, 0.55, 0.45, 0.6), (0.05, -0.95, 0.75, 0.45)):
        l.add(ELI, c + d3 * a + lat * b + [0, 0, h], rot_z(rng.uniform(0, 3.1)), [s * 1.1, s * 0.9, s * 0.8],
              gris(rng.uniform(0.33, 0.45)), patron=2)
    l.cerrar_grupo(); ocup.append((c[0], c[1], 2.3)); roca_c = c
    # Aguja de roca junto a E1: queda a menos de 0,2 m del morro a mitad del giro E4 (se ignora).
    semi = np.array([0.36, 0.33, 2.3]); R = rot_z(0.3); d3 = np.array([0.0, 1.0, 0.0])
    nariz = np.array([E1[0], E1[1], z75]) + 0.335 * d3
    c = np.array([E1[0], E1[1] + 1.2, Z_FONDO + 1.95])
    c = c + d3 * (0.17 - _t_eli(c, R * semi, nariz, d3))
    l.add(ELI, c, R, semi, gris(0.43), patron=2)
    l.add(ELI, c + [0.25, 0.50, -1.75], rot_z(1.0), [0.60, 0.50, 0.55], gris(0.38), patron=2)
    l.add(ELI, c + [-0.35, 0.55, -1.90], rot_z(2.0), [0.50, 0.45, 0.40], gris(0.35), patron=2)
    l.add(ELI, c + [0.05, 0.34, -1.00], rot_z(0.5), [0.30, 0.30, 0.90], gris(0.40), patron=2)
    l.add(ELI, c + [0.10, 0.22, 0.90], rot_z(0.9), [0.22, 0.24, 0.75], gris(0.41), patron=2)
    l.add(ELI, c + [-0.30, -0.25, -1.95], rot_z(0.2), [0.45, 0.40, 0.35], gris(0.36), patron=2)
    l.add(ELI, c + [0.45, -0.20, -2.00], rot_z(1.4), [0.40, 0.35, 0.30], gris(0.39), patron=2)
    l.cerrar_grupo(); ocup.append((c[0], c[1] + 0.3, 1.0)); aguja_c = c
    # Rocas dispersas y rocas del arrecife
    rocas = []
    for _ in range(11):
        for _ in range(60):
            xy = rng.uniform(-5.6, 5.6, 2)
            if all(math.hypot(xy[0] - ox, xy[1] - oy) > rr + 0.7 for ox, oy, rr in ocup):
                break
        tam = rng.uniform(0.35, 0.8)
        roca(l, xy, tam, rng); rocas.append((xy, tam)); ocup.append((xy[0], xy[1], tam * 1.3))
    for _ in range(5):
        xy = ARRECIFE + rng.normal(0, 0.9, 2); tam = rng.uniform(0.4, 0.75)
        roca(l, xy, tam, rng); rocas.append((xy, tam))
    # Corales, esponjas, abanicos y anémonas junto a las rocas y en el arrecife
    sitios = []
    for xy, tam in rocas:
        for _ in range(int(rng.integers(1, 4))):
            ang = rng.uniform(0, 2 * math.pi); dd = tam * rng.uniform(0.7, 1.5)
            sitios.append(xy + dd * np.array([math.cos(ang), math.sin(ang)]))
    for _ in range(16):
        ang = rng.uniform(0, 2 * math.pi); dd = rng.uniform(0.2, 2.1)
        sitios.append(ARRECIFE + dd * np.array([math.cos(ang), math.sin(ang)]))
    for a, b in ((1.4, 1.2), (1.9, -0.9), (2.4, 0.3), (0.9, -1.6)):           # detrás de la roca del aborto
        sitios.append(roca_c[:2] + DIR_ABORTO * a + np.array([-DIR_ABORTO[1], DIR_ABORTO[0]]) * b)
    for a, b in ((0.5, 0.9), (-0.6, 0.8), (0.1, 1.2)):                        # detrás de la aguja
        sitios.append(aguja_c[:2] + np.array([a, b]))
    for xy in sitios:
        if _dist_polilinea(xy, RUTA2) < 0.75:
            continue
        base = [xy[0], xy[1], apoyo(l, xy[0], xy[1]) - 0.02]
        col = CORALES[int(rng.integers(len(CORALES)))]
        tipo = rng.integers(5)
        if base[2] > -7.9 and _dist_polilinea(xy, RUTA2) < 1.6:               # nada alto junto al recorrido 2
            tipo = 1
        if tipo == 0:
            coral_ramas(l, base, rng.uniform(0.45, 0.85), col, rng)
        elif tipo == 1:
            coral_cerebro(l, base, rng.uniform(0.16, 0.34), col, rng)
        elif tipo == 2:
            esponjas(l, base, rng.uniform(0.35, 0.65), col, rng)
        elif tipo == 3:
            abanico(l, base, rng.uniform(0.5, 0.8), col, rng)
        else:
            anemona(l, base, rng.uniform(0.07, 0.11), col, rng)
    for _ in range(6):                                                         # estrellas de mar
        xy = rng.uniform(-4.5, 4.5, 2)
        estrella(l, [xy[0], xy[1], apoyo(l, xy[0], xy[1])], rng.uniform(0.12, 0.18),
                 CORALES[int(rng.integers(3))], rng)
    for _ in range(36):                                                        # guijarros y conchas
        xy = rng.uniform(-5.8, 5.8, 2); s = rng.uniform(0.03, 0.08)
        l.add(ELI, [xy[0], xy[1], apoyo(l, xy[0], xy[1]) + 0.3 * s], rot_z(rng.uniform(0, 3.1)),
              [s * rng.uniform(1.0, 1.6), s, s * 0.6], gris(rng.uniform(0.35, 0.75)))
        l.cerrar_grupo()
    # Poste de escala (cajas): mástil, base y una placa por marca
    l.add(CAJA, [POSTE[0], POSTE[1], Z_FONDO / 2], I3, [0.045, 0.045, -Z_FONDO / 2], [0.86, 0.86, 0.83])
    l.add(CAJA, [POSTE[0], POSTE[1], Z_FONDO + 0.08], I3, [0.30, 0.30, 0.08], [0.45, 0.45, 0.45])
    for k, m in enumerate(MARCAS):
        l.add(CAJA, [POSTE[0], POSTE[1], -max(m, 0.04)], I3, [0.17, 0.17, 0.04],
              [0.95, 0.22, 0.15] if k % 2 == 0 else [0.95, 0.95, 0.95])
    l.cerrar_grupo()
    return l.matrices(), ocup


FIJOS, _OCUPADO = _fijos()

# ----------------------------------------------------------------------------------
# VIDA MARINA: algas, hierba, peces, medusas, burbujas y partículas
# ----------------------------------------------------------------------------------
N_ALGAS = 30
W_ALGA = 2 * math.pi * 8 / T_ANIM          # 8 oscilaciones por bucle (5 s): cierra sin salto
K_ALGA = 1.1                               # desfase por metro de altura (rad/m)


def _algas():
    rng = RNG
    fijas = [(-0.3, 1.6, 3.8), (1.25, 0.9, 2.3), (0.6, -1.9, 2.2)]            # junto a las rutas, para las sombras
    algas = []
    while len(algas) < N_ALGAS:
        if len(algas) < len(fijas):
            xy, alto = np.array(fijas[len(algas)][:2]), fijas[len(algas)][2]
        else:
            xy, alto = rng.uniform(-5.7, 5.7, 2), rng.uniform(2.0, 4.0)
            if xy[1] < -1.6:
                alto = min(alto, rng.uniform(2.0, 2.6))                       # primer plano: no tapar al dron
            d2 = _dist_polilinea(xy, RUTA2)
            if d2 < (1.9 if alto > 2.5 else 1.0) or np.linalg.norm(xy - ARRECIFE) < 2.5:
                continue
            if any(math.hypot(xy[0] - ox, xy[1] - oy) < rr + 0.45 for ox, oy, rr in _OCUPADO):
                continue
            if any(np.linalg.norm(xy - a["xy"]) < 0.8 for a in algas):
                continue
        n = int(rng.integers(6, 9)); ang = rng.uniform(0, 2 * math.pi); v = rng.uniform(0, 1)
        algas.append(dict(
            xy=xy, alto=alto, n=n, fase=rng.uniform(0, 2 * math.pi), amp=rng.uniform(0.22, 0.40),
            u=np.array([math.cos(ang), math.sin(ang)]), incl=rng.uniform(-0.22, 0.22), giro=rng.uniform(0, 6.28),
            tallo=np.array([0.20 + 0.10 * v, 0.30 + 0.06 * v, 0.08]),
            hoja=np.array([0.16 + 0.22 * v, 0.50 + 0.18 * rng.uniform(), 0.07 + 0.08 * (1 - v)]),
            hojas=[[(rng.uniform(0, 6.28), rng.uniform(0.38, 0.62), rng.uniform(0.055, 0.085)) for _ in range(2)]
                   for _ in range(n)]))
    return algas


ALGAS = _algas()


def juntas_alga(a, t):
    hs = np.linspace(0, a["alto"], a["n"] + 1)
    fr = (hs / a["alto"]) ** 1.6                                   # amplitud creciente hacia la punta
    lat = a["amp"] * fr * np.sin(W_ALGA * t + a["fase"] + K_ALGA * hs)
    lat2 = 0.4 * a["amp"] * fr * np.sin(W_ALGA * t + a["fase"] + 1.3 + K_ALGA * hs)
    u, v = a["u"], np.array([-a["u"][1], a["u"][0]])
    xy = a["xy"] + np.outer(lat + a["incl"] * fr, u) + np.outer(lat2, v)
    return np.column_stack([xy, Z_FONDO + hs])


def lista_algas(t):
    l = Lista()
    for a in ALGAS:
        J = juntas_alga(a, t); n = a["n"]
        for j in range(n):
            p, q = J[j], J[j + 1]
            tramo(l, p, q, 0.028, 0.028, a["tallo"], trans=0.2)
            ex = (q - p) / np.linalg.norm(q - p)
            for k, (ang, largo, ancho) in enumerate(a["hojas"][j]):           # dos hojas por nudo
                ang = ang + 0.22 * math.sin(W_ALGA * t + a["fase"] + K_ALGA * (j + 1) + 1.0 + k)
                d = 0.55 * ex + 0.85 * np.array([math.cos(ang), math.sin(ang), 0.0])
                d /= np.linalg.norm(d)
                l.add(ELI, q + d * largo * 0.48, base_eje(d), [0.5 * largo, ancho, 0.012],
                      a["hoja"] * (0.85 + 0.30 * j / n), trans=0.55, patron=4)
        d = J[-1] - J[-2]; d /= np.linalg.norm(d)
        l.add(ELI, J[-1] + d * 0.22, base_eje(d, a["giro"]), [0.30, 0.085, 0.012], a["hoja"] * 1.15, trans=0.55, patron=4)
        l.cerrar_grupo()
    return l


def _hierba():
    rng = RNG; matas = []
    while len(matas) < 20:
        xy = rng.uniform(-5.6, 5.6, 2)
        if any(math.hypot(xy[0] - ox, xy[1] - oy) < rr + 0.2 for ox, oy, rr in _OCUPADO):
            continue
        g = rng.uniform(0.55, 0.85)
        matas.append(dict(xy=xy, col=np.array([0.22, g, 0.16]), hojas=[
            (xy + rng.normal(0, 0.10, 2), rng.uniform(0, 6.28), rng.uniform(0.35, 0.85), rng.uniform(0.05, 0.35),
             rng.uniform(0, 6.28)) for _ in range(int(rng.integers(7, 11)))]))
    return matas


HIERBA = _hierba()
W_HIERBA = 2 * math.pi * 12 / T_ANIM


def lista_hierba(t):
    l = Lista()
    for m in HIERBA:
        for (xy, ang, alto, incl, fase) in m["hojas"]:
            a = incl + 0.16 * math.sin(W_HIERBA * t + fase)
            d = np.array([math.sin(a) * math.cos(ang), math.sin(a) * math.sin(ang), math.cos(a)])
            l.add(ELI, np.array([xy[0], xy[1], Z_FONDO]) + d * alto * 0.48, base_eje(d, ang), [0.5 * alto, 0.022, 0.006],
                  m["col"], trans=0.5, patron=4)
        l.cerrar_grupo()
    return l


BANCOS = [  # peces en bancos que recorren elipses; vueltas enteras por bucle
    dict(c=(0.3, 4.4), a=4.6, b=1.2, z=-4.3, vueltas=1, n=12, L=0.22, col=(1.00, 0.76, 0.10), patron=5),
    dict(c=(4.6, 0.5), a=1.1, b=2.4, z=-5.5, vueltas=-1, n=9, L=0.18, col=(0.35, 0.62, 0.98), patron=5),
    dict(c=(ARRECIFE[0], ARRECIFE[1]), a=2.0, b=1.1, z=-9.2, vueltas=2, n=8, L=0.15, col=(1.00, 0.42, 0.08), patron=1),
    dict(c=(0.0, 4.9), a=6.0, b=1.2, z=-5.6, vueltas=-1, n=2, L=0.46, col=(0.55, 0.62, 0.70), patron=5),
    dict(c=(2.0, 4.2), a=2.4, b=1.1, z=-1.9, vueltas=2, n=26, L=0.12, col=(0.74, 0.82, 0.90), patron=5),
]
for _b in BANCOS:
    _b["fase"] = RNG.uniform(0, 2 * math.pi)
    _b["peces"] = [dict(da=RNG.uniform(-0.28, 0.28), dr=RNG.uniform(-0.4, 0.4), dz=RNG.uniform(-0.25, 0.25),
                        f=RNG.uniform(0, 2 * math.pi), k=int(RNG.integers(2, 6))) for _ in range(_b["n"])]


def lista_peces(t):
    l = Lista(); w = 2 * math.pi * t / T_ANIM
    for b in BANCOS:
        L = b["L"]; col = np.array(b["col"]); sg = 1 if b["vueltas"] > 0 else -1
        for p in b["peces"]:
            al = b["fase"] + b["vueltas"] * w + p["da"] + 0.04 * math.sin(3 * w + p["f"])
            a = b["a"] + p["dr"]; bb = b["b"] + p["dr"]
            c = np.array([b["c"][0] + a * math.cos(al), b["c"][1] + bb * math.sin(al),
                          b["z"] + p["dz"] + 0.10 * math.sin(p["k"] * w + p["f"])])
            bat = 2 * math.pi * 2.5 * t + p["f"]                              # coletazo: 100 ciclos por bucle
            Rz = rot_z(math.atan2(bb * math.cos(al) * sg, -a * math.sin(al) * sg) + 0.10 * math.sin(bat + 1.2))
            l.add(ELI, c, Rz, [0.5 * L, 0.13 * L, 0.21 * L], col, esp=0.6, patron=b["patron"], emi=tuple(col * 0.05))
            Rc = Rz @ rot_z(0.5 * math.sin(bat))
            l.add(ELI, c + Rz @ np.array([-0.43 * L, 0, 0]) + Rc @ np.array([-0.17 * L, 0, 0]), Rc,
                  [0.17 * L, 0.02 * L, 0.19 * L], col * 0.85, trans=0.5)
            l.add(ELI, c + Rz @ np.array([-0.02 * L, 0, 0.2 * L]), Rz @ rot_y(0.5), [0.16 * L, 0.015 * L, 0.09 * L],
                  col * 0.8, trans=0.5)
            for sy in (1, -1):                                                # ojos
                l.add(ELI, c + Rz @ np.array([0.30 * L, 0.092 * L * sy, 0.045 * L]), I3, [0.042 * L] * 3, [0.03, 0.03, 0.04], esp=1.0)
        l.cerrar_grupo()
    return l


TORTUGA = dict(c=(-0.5, 3.9), a=5.0, b=1.0, z=-3.0, fase=RNG.uniform(0, 2 * math.pi))


def lista_tortuga(t):
    """Tortuga que da una vuelta por bucle al fondo de la escena, con aleteo periódico."""
    l = Lista(); w = 2 * math.pi * t / T_ANIM; T = TORTUGA
    al = T["fase"] + w
    c = np.array([T["c"][0] + T["a"] * math.cos(al), T["c"][1] + T["b"] * math.sin(al), T["z"] + 0.25 * math.sin(3 * w)])
    Rz = rot_z(math.atan2(T["b"] * math.cos(al), -T["a"] * math.sin(al))) @ rot_y(-0.10 * math.cos(3 * w))
    ale = 0.55 * math.sin(20 * w)                                   # 20 aleteos por bucle
    capa, piel = np.array([0.36, 0.33, 0.15]), np.array([0.50, 0.55, 0.32])

    def e(p, Rl, semi, alb, **m):
        l.add(ELI, c + Rz @ np.array(p, float), Rz @ Rl, semi, alb, **m)
    e([0, 0, 0.02], I3, [0.30, 0.24, 0.10], capa, patron=3, esp=0.3)
    e([0, 0, -0.03], I3, [0.28, 0.22, 0.07], piel * 1.25)
    e([0.37, 0, 0.0], I3, [0.09, 0.065, 0.055], piel)
    for sgn in (1, -1):
        Rf = rot_x(sgn * (0.15 + ale)) @ rot_z(sgn * 1.05)
        e(np.array([0.17, 0.20 * sgn, -0.01]) + Rf @ np.array([0.20, 0, 0]), Rf, [0.24, 0.07, 0.014], piel, trans=0.2)
        Rt = rot_x(sgn * 0.3 * ale) @ rot_z(sgn * 2.5)
        e(np.array([-0.24, 0.13 * sgn, -0.02]) + Rt @ np.array([0.09, 0, 0]), Rt, [0.11, 0.05, 0.012], piel, trans=0.2)
    l.cerrar_grupo()
    return l


MEDUSAS = [dict(c=(4.8, 2.8, -3.2), col=(0.95, 0.55, 0.85), f=0.4), dict(c=(-4.9, 3.4, -4.6), col=(0.60, 0.70, 1.00), f=2.1),
           dict(c=(2.9, 4.9, -6.0), col=(0.95, 0.75, 0.55), f=4.0)]


def lista_medusas(t):
    l = Lista(); w = 2 * math.pi * t / T_ANIM
    for m in MEDUSAS:
        col = np.array(m["col"]); pul = math.sin(16 * w + m["f"])
        c = np.array(m["c"]) + [0.15 * math.sin(w + m["f"]), 0.15 * math.cos(2 * w + m["f"]), 0.35 * math.sin(2 * w + m["f"])]
        r = 0.20 * (1 + 0.12 * pul); h = 0.13 * (1 - 0.15 * pul)
        l.add(ELI, c, I3, [r, r, h], col, trans=0.85, emi=tuple(col * 0.10), esp=0.3)
        l.add(ELI, c + [0, 0, -0.03], I3, [0.6 * r, 0.6 * r, 0.75 * h], col * 1.1, trans=0.85, emi=tuple(col * 0.16))
        for k in range(6):
            ang = 2 * math.pi * k / 6
            p = c + [0.7 * r * math.cos(ang), 0.7 * r * math.sin(ang), -0.5 * h]
            for j in range(4):
                q = p + [0.045 * (j + 1) * math.sin(8 * w + m["f"] + 0.9 * j + k),
                         0.045 * (j + 1) * math.cos(8 * w + m["f"] + 0.7 * j + 2 * k), -0.17]
                tramo(l, p, q, 0.011, 0.011, col * 0.9, trans=0.8, emi=tuple(col * 0.06))
                p = q
        l.cerrar_grupo()
    return l


BURBUJAS = [[dict(s0=RNG.uniform(0, 1), r=RNG.uniform(0.012, 0.03), f1=RNG.uniform(0, 6.28), f2=RNG.uniform(0, 6.28),
                  k1=int(RNG.integers(20, 40)), k2=int(RNG.integers(20, 40))) for _ in range(16)] for _ in RESPIRADEROS]


def lista_burbujas(t):
    l = Lista(); w = 2 * math.pi * t / T_ANIM
    for (vx, vy), bs in zip(RESPIRADEROS, BURBUJAS):
        for b in bs:
            s = (2 * t / T_ANIM + b["s0"]) % 1.0                              # dos subidas por bucle
            a = 0.04 + 0.22 * s
            l.add(ELI, [vx + a * math.sin(b["k1"] * w + b["f1"]), vy + a * math.sin(b["k2"] * w + b["f2"]),
                        Z_FONDO + 0.1 + s * (-Z_FONDO - 0.14)], I3, [b["r"] * (1 + 0.9 * s)] * 3, [0.85, 0.97, 1.0],
                  refl=0.65, esp=1.0, solido=0)
        l.cerrar_grupo()
    return l


N_PART = 2600                               # partículas en suspensión ("nieve marina"), en espacio de pantalla
_P0 = RNG.uniform([-9, -10, -10.3], [9, 8, -0.2], (N_PART, 3))
_PAMP = RNG.uniform(0.10, 0.35, (N_PART, 3)); _PFAS = RNG.uniform(0, 6.28, (N_PART, 3))
_PFRE = RNG.integers(1, 4, (N_PART, 3)).astype(float)
_PTAM = RNG.uniform(0.004, 0.010, N_PART); _PBRI = RNG.uniform(0.4, 1.0, N_PART)


# ----------------------------------------------------------------------------------
# DRON Y ESTELA
# ----------------------------------------------------------------------------------
def lista_dron(pos, rumbo, ang_v, ang_ri, ang_rd):
    """Dron siempre horizontal. Local: x = morro, y = izquierda, z = arriba. Rumbo horario positivo."""
    Rz = rot_z(-math.radians(rumbo))
    l = Lista()

    def e(c, Rl, semi, alb, **m):
        l.add(ELI, pos + Rz @ np.array(c, float), Rz @ Rl, semi, alb, **m)
    am = [0.96, 0.66, 0.08]
    e([0, 0, 0], I3, [0.30, 0.175, 0.10], am, refl=0.40, esp=1.0)                    # cuerpo metálico
    e([0.06, 0, 0.075], I3, [0.13, 0.085, 0.055], [0.08, 0.12, 0.18], refl=0.60, esp=1.0)   # cúpula
    e([0.285, 0, -0.005], I3, [0.04, 0.045, 0.045], [1, 1, 0.9], emi=(2.8, 2.7, 2.2))       # foco
    e([-0.20, 0, 0.088], rot_y(0.35), [0.10, 0.006, 0.035], am, esp=0.5)             # aleta dorsal
    osc, claro, rojo = [0.15, 0.16, 0.19], [0.95, 0.95, 0.95], [0.95, 0.24, 0.12]
    for s in (1, -1):                                                               # patines
        e([0.02, 0.12 * s, -0.115], I3, [0.22, 0.013, 0.013], osc)
        e([0.12, 0.12 * s, -0.085], I3, [0.012, 0.012, 0.035], osc)
        e([-0.10, 0.12 * s, -0.085], I3, [0.012, 0.012, 0.035], osc)
    for s, ang in ((1, ang_v), (-1, -ang_v)):                                       # hélices verticales (PWM0)
        e([0, 0.23 * s, 0.0], I3, [0.035, 0.10, 0.022], osc)
        e([0, 0.31 * s, 0.03], I3, [0.028, 0.028, 0.035], osc)
        e([0, 0.31 * s, 0.062], rot_z(ang), [0.105, 0.022, 0.008], claro)
        e([0, 0.31 * s, 0.062], rot_z(ang + math.pi / 2), [0.105, 0.022, 0.008], rojo)
    for s, ang in ((1, ang_ri), (-1, ang_rd)):                                      # hélices traseras (PWM1)
        e([-0.31, 0.10 * s, 0], I3, [0.05, 0.022, 0.022], osc)
        e([-0.365, 0.10 * s, 0], rot_x(ang), [0.008, 0.078, 0.018], claro)
        e([-0.365, 0.10 * s, 0], rot_x(ang + math.pi / 2), [0.008, 0.078, 0.018], rojo)
    l.cerrar_grupo()
    inc = math.radians(FOCO_INCL)
    return l, pos + Rz @ np.array([0.40, 0, 0]), Rz @ np.array([math.cos(inc), 0, -math.sin(inc)])


COL_ESTELA = {"vertical": (0.35, 0.88, 1.00), "avance": (1.00, 0.80, 0.24), "giro": (1.00, 0.47, 0.80)}


def lista_estela(i):
    """Estela del recorrido en curso: cajas finas emisivas (no dan sombra). Se desvanece al terminar."""
    l = Lista(); e = EST[i]; f = e["fase"]; ts = e["ts"]
    if f["tipo"] == "reposo":
        if f["n"] == 0:
            return l
        rec = (f["rec"] - 1) % len(RECORRIDOS); g = 1.0 - float(suave((ts - f["t0"]) / 0.8)); tfin = f["t0"]
    else:
        rec, g, tfin = f["rec"], 1.0, ts
    if g <= 0.02:
        return l
    r = 0.012 * g

    def linea(p, q, col):
        d = q - p; lon = np.linalg.norm(d)
        if lon > 1e-4:
            l.add(CAJA, (p + q) / 2, base_eje(d), [lon / 2 + r, r, r], (0, 0, 0), emi=tuple(1.7 * c for c in col), solido=0)
    for ff in FASES:
        if ff["rec"] != rec or ff["tipo"] == "reposo" or ff["t0"] >= tfin:
            continue
        u = min(1.0, (tfin - ff["t0"]) / (ff["t1"] - ff["t0"])); col = COL_ESTELA[ff["tipo"]]
        p = np.array([ff["pos0"][0], ff["pos0"][1], z_dron(ff["prof0"])])
        if ff["tipo"] == "giro":                                   # arco horizontal que traza el morro
            a0 = -math.radians(ff["rumbo0"]); a1 = -math.radians(ff["rumbo0"] + (ff["rumbo1"] - ff["rumbo0"]) * u)
            aa = np.linspace(a0, a1, max(2, int(abs(a1 - a0) / 0.2) + 2))
            pts = p + 0.33 * np.column_stack([np.cos(aa), np.sin(aa), np.zeros(len(aa))])
            for a, b in zip(pts[:-1], pts[1:]):
                linea(a, b, col)
        elif ff["tipo"] == "vertical":
            linea(p, np.array([p[0], p[1], z_dron(ff["prof0"] + (ff["prof1"] - ff["prof0"]) * float(suave(u)))]), col)
        else:
            xy = ff["pos0"] + (ff["pos1"] - ff["pos0"]) * u
            linea(p, np.array([xy[0], xy[1], p[2]]), col)
    l.cerrar_grupo()
    return l


def escena(i, con_estela=True):
    t = i / FPS
    ld, foco_pos, foco_dir = lista_dron(POS[i], EST[i]["rumbo"], ANG_V[i], ANG_RI[i], ANG_RD[i])
    partes = [FIJOS, lista_algas(t).matrices(), lista_hierba(t).matrices(), lista_peces(t).matrices(),
              lista_tortuga(t).matrices(), lista_medusas(t).matrices(), lista_burbujas(t).matrices()]
    n0 = sum(len(p["C"]) for p in partes)
    partes.append(ld.matrices())
    if con_estela:
        est = lista_estela(i).matrices()
        if est is not None:
            partes.append(est)
    esc = Escena(partes, foco_pos, foco_dir, t)
    esc.dron = (n0, n0 + len(ld.C))
    esc.frente = distancia_frente(esc, i)
    esc.foco_rango = min(5.0, esc.frente + 0.55)
    return esc


# ----------------------------------------------------------------------------------
# RAYTRACER
# ----------------------------------------------------------------------------------
_CERCA = 0.3                                                    # plano cercano (m): no hay nada tan cerca de la cámara
_ARI = np.array([(a, b) for a in range(8) for b in range(a + 1, 8) if bin(a ^ b).count("1") == 1])


def visibilidad(esc, cam):
    """Rayos primarios: cada primitiva solo se prueba en el rectángulo de pantalla que la contiene."""
    pos, f, r, u = cam
    dz = f[2] + XS[None, :] * r[2] + YS[:, None] * u[2]
    dz = np.where(np.abs(dz) < 1e-9, 1e-9, dz)
    tbuf = np.where(dz > 0, -pos[2] / dz, (Z_FONDO - pos[2]) / dz)             # superficie o fondo
    idbuf = np.where(dz > 0, -2, -3).astype(np.int32)
    v = esc.esq - pos
    zc = v @ f; xc = v @ r; yc = v @ u
    dl = zc > _CERCA
    alguno = dl.any(1)
    zs = np.where(dl, zc, 1.0)
    px = np.where(dl, W / 2 + FOCAL * xc / zs, np.nan); py = np.where(dl, H / 2 - FOCAL * yc / zs, np.nan)
    # aristas de la caja que cruzan el plano cercano: su corte también acota el rectángulo
    za, zb = zc[:, _ARI[:, 0]], zc[:, _ARI[:, 1]]
    cr = (za > _CERCA) != (zb > _CERCA)
    sc = np.where(cr, (_CERCA - za) / np.where(cr, zb - za, 1.0), 0.0)
    xi = xc[:, _ARI[:, 0]] + sc * (xc[:, _ARI[:, 1]] - xc[:, _ARI[:, 0]])
    yi = yc[:, _ARI[:, 0]] + sc * (yc[:, _ARI[:, 1]] - yc[:, _ARI[:, 0]])
    px = np.concatenate([px, np.where(cr, W / 2 + FOCAL * xi / _CERCA, np.nan)], 1)
    py = np.concatenate([py, np.where(cr, H / 2 - FOCAL * yi / _CERCA, np.nan)], 1)
    px[~alguno] = 0.0; py[~alguno] = 0.0
    x0 = np.clip(np.floor(np.nanmin(px, 1)) - 1, 0, W).astype(int); x1 = np.clip(np.ceil(np.nanmax(px, 1)) + 2, 0, W).astype(int)
    y0 = np.clip(np.floor(np.nanmin(py, 1)) - 1, 0, H).astype(int); y1 = np.clip(np.ceil(np.nanmax(py, 1)) + 2, 0, H).astype(int)
    Mo = np.einsum("kij,kj->ki", esc.M, pos - esc.C)
    Mf = esc.M @ f; Mr = esc.M @ r; Mu = esc.M @ u
    qc = (Mo * Mo).sum(1) - 1.0
    esc.Mo = Mo                                                    # origen de la cámara en local (lo reutiliza el AA)
    tipo = esc.tipo
    for k in np.flatnonzero(alguno & (x1 > x0) & (y1 > y0)):
        xa, xb, ya, yb = x0[k], x1[k], y0[k], y1[k]
        X = XS[None, xa:xb]; Y = YS[ya:yb, None]
        d0 = Mf[k, 0] + Mr[k, 0] * X + Mu[k, 0] * Y
        d1 = Mf[k, 1] + Mr[k, 1] * X + Mu[k, 1] * Y
        d2 = Mf[k, 2] + Mr[k, 2] * X + Mu[k, 2] * Y
        tb = tbuf[ya:yb, xa:xb]
        if tipo[k] == ELI:                                         # esfera unidad en local
            qa = d0 * d0 + d1 * d1 + d2 * d2
            qb = Mo[k, 0] * d0 + Mo[k, 1] * d1 + Mo[k, 2] * d2
            disc = qb * qb - qa * qc[k]
            m = disc > 0
            if not m.any():
                continue
            t = (-qb - np.sqrt(np.where(m, disc, 0.0))) / qa
        else:                                                      # cubo unidad en local
            tn = tf = None
            for o, d in ((Mo[k, 0], d0), (Mo[k, 1], d1), (Mo[k, 2], d2)):
                inv = 1.0 / np.where(np.abs(d) < 1e-12, 1e-12, d)
                a = (-1.0 - o) * inv; b = (1.0 - o) * inv
                lo = np.minimum(a, b); hi = np.maximum(a, b)
                tn = lo if tn is None else np.maximum(tn, lo); tf = hi if tf is None else np.minimum(tf, hi)
            m = tf >= tn; t = tn
        m &= (t > _CERCA) & (t < tb)
        if m.any():
            tb[m] = t[m]; idbuf[ya:yb, xa:xb][m] = k
    return tbuf, idbuf


def _inter_grupo(esc, a, b, tipo, o, d):
    """Distancias (n rayos x k primitivas) del grupo; inf si no hay corte."""
    M = esc.M[a:b]
    ol = np.einsum("kij,nkj->nki", M, o[:, None, :] - esc.C[None, a:b])
    dl = np.einsum("kij,nj->nki", M, d)
    with np.errstate(divide="ignore", invalid="ignore"):
        if tipo == ELI:
            qa = (dl * dl).sum(-1); qb = (ol * dl).sum(-1); qc = (ol * ol).sum(-1) - 1.0
            disc = qb * qb - qa * qc
            ok = disc > 0
            tt = (-qb - np.sqrt(np.where(ok, disc, 0.0))) / qa
            tt[~ok | (tt < EPS)] = np.inf
        else:
            inv = 1.0 / np.where(np.abs(dl) < 1e-12, 1e-12, dl)
            t0 = (-1.0 - ol) * inv; t1 = (1.0 - ol) * inv
            tn = np.minimum(t0, t1).max(-1); tf = np.maximum(t0, t1).min(-1)
            tt = np.where((tf >= tn) & (tn > EPS), tn, np.inf)
    return tt


def intersecar(esc, O, D, solo_solidos=False):
    """Corte más cercano de rayos cualesquiera. Devuelve t e id (-2 superficie, -3 fondo, >= 0 primitiva)."""
    dz = np.where(np.abs(D[:, 2]) < 1e-9, 1e-9, D[:, 2])
    t = np.where(dz > 0, -O[:, 2] / dz, (Z_FONDO - O[:, 2]) / dz)
    t = np.where(t > EPS, t, 1e6)
    pid = np.where(dz > 0, -2, -3).astype(np.int32)
    inv = 1.0 / np.where(np.abs(D) < 1e-9, 1e-9, D)
    q0 = (esc.lo - O) * inv; q1 = (esc.hi - O) * inv                # recorte al volumen con objetos
    te = np.maximum(np.minimum(q0, q1).max(1), 0.0); ts = np.minimum(np.maximum(q0, q1).min(1), t)
    act = np.flatnonzero(ts > te)
    if act.size == 0:
        return t, pid
    Oa = O[act]; Da = D[act]; inva = inv[act]
    P0 = Oa + Da * te[act, None]; P1 = Oa + Da * ts[act, None]
    m = Da[:, :2].mean(0); ang = math.atan2(m[1], m[0]); c, s = math.cos(ang), math.sin(ang)

    def gira(Q):                                                   # marco alineado con el haz de rayos
        return Q[..., 0] * c + Q[..., 1] * s, Q[..., 1] * c - Q[..., 0] * s
    ax, ay = gira(P0); bx, by = gira(P1); gx, gy = gira(esc.gesq)
    cand = ((gx.max(1) >= min(ax.min(), bx.min())) & (gx.min(1) <= max(ax.max(), bx.max())) &
            (gy.max(1) >= min(ay.min(), by.min())) & (gy.min(1) <= max(ay.max(), by.max())) &
            (esc.ghi[:, 2] >= min(P0[:, 2].min(), P1[:, 2].min())) & (esc.glo[:, 2] <= max(P0[:, 2].max(), P1[:, 2].max())))
    if solo_solidos:
        cand &= esc.gsol
    ta = t[act]; pa = pid[act]
    for gi in np.flatnonzero(cand):
        q0 = (esc.glo[gi] - Oa) * inva; q1 = (esc.ghi[gi] - Oa) * inva
        tn = np.minimum(q0, q1).max(1); tf = np.maximum(q0, q1).min(1)
        idx = np.flatnonzero((tf >= np.maximum(tn, 0.0)) & (tn < ta))
        if idx.size == 0:
            continue
        a = esc.ga[gi]
        tt = _inter_grupo(esc, a, esc.gb[gi], esc.gtipo[gi], Oa[idx], Da[idx])
        j = tt.argmin(1); tm = tt[np.arange(idx.size), j]
        mej = tm < ta[idx]
        ta[idx[mej]] = tm[mej]; pa[idx[mej]] = a + j[mej]
    t[act] = ta; pid[act] = pa
    return t, pid


def distancia_frente(esc, i):
    """Lo que mediría el sónar: distancia del morro al primer obstáculo sólido en la dirección del rumbo."""
    r = math.radians(EST[i]["rumbo"]); d = np.array([math.cos(r), -math.sin(r), 0.0])
    t, pid = intersecar(esc, (POS[i] + 0.335 * d)[None], d[None], solo_solidos=True)
    return float(t[0]) if pid[0] >= 0 else 99.0


def sombra_sol(esc, P, propio):
    """Sombras duras del sol. Los puntos se clasifican en el "espacio del sol" (u, v) para probar solo
    las primitivas que pueden taparlos. propio = id de la primitiva de cada punto (no se da sombra a sí misma)."""
    u = P[:, 0] - P[:, 2] * KX; v = P[:, 1] - P[:, 2] * KY; z = P[:, 2]
    tap = np.zeros(len(P), bool)
    cand = np.flatnonzero(esc.gsol & (esc.gu1 >= u.min()) & (esc.gu0 <= u.max()) & (esc.gv1 >= v.min()) &
                          (esc.gv0 <= v.max()) & (esc.gz1 > z.min()))
    for gi in cand:
        idx = np.flatnonzero((u >= esc.gu0[gi]) & (u <= esc.gu1[gi]) & (v >= esc.gv0[gi]) & (v <= esc.gv1[gi]) &
                             (z < esc.gz1[gi]) & ~tap)
        if idx.size == 0:
            continue
        a, b = esc.ga[gi], esc.gb[gi]
        ui = u[idx, None]; vi = v[idx, None]
        mm = ((ui >= esc.pu0[a:b]) & (ui <= esc.pu1[a:b]) & (vi >= esc.pv0[a:b]) & (vi <= esc.pv1[a:b]) &
              (propio[idx, None] != np.arange(a, b)))
        ri, pj = np.nonzero(mm)
        if ri.size == 0:
            continue
        pk = a + pj; ro = idx[ri]
        ol = np.einsum("pij,pj->pi", esc.M[pk], P[ro] - esc.C[pk]); dl = esc.ML[pk]
        if esc.gtipo[gi] == ELI:
            qa = esc.qaL[pk]; qb = (ol * dl).sum(1)
            disc = qb * qb - qa * ((ol * ol).sum(1) - 1.0)
            hit = (disc > 0) & (qb < 0)
            hit[hit] = (-qb[hit] - np.sqrt(disc[hit])) > EPS * qa[hit]
        else:
            inv = 1.0 / np.where(np.abs(dl) < 1e-12, 1e-12, dl)
            t0 = (-1.0 - ol) * inv; t1 = (1.0 - ol) * inv
            tn = np.minimum(t0, t1).max(1); tf = np.maximum(t0, t1).min(1)
            hit = (tf >= tn) & (tn > EPS)
        tap[ro[hit]] = True
    return tap


def sombra_luz(esc, P):
    """Sombras duras del foco. Los puntos se clasifican en la proyección del foco para probar solo las
    primitivas que pueden quedar entre el foco y cada punto."""
    vp = P - esc.foco_pos
    wp = vp @ esc.foco_dir
    ws = np.maximum(wp, 1e-6)
    ap = (vp @ esc.fe1) / ws; bp = (vp @ esc.fe2) / ws
    dist = np.linalg.norm(vp, axis=1); D = -vp / dist[:, None]     # del punto hacia el foco
    lim = dist - 0.03
    tap = np.zeros(len(P), bool)
    cand = np.flatnonzero(esc.gsol & (esc.gfa1 >= ap.min()) & (esc.gfa0 <= ap.max()) & (esc.gfb1 >= bp.min()) &
                          (esc.gfb0 <= bp.max()) & (esc.gfw0 < wp.max()))
    for gi in cand:
        idx = np.flatnonzero((ap >= esc.gfa0[gi]) & (ap <= esc.gfa1[gi]) & (bp >= esc.gfb0[gi]) & (bp <= esc.gfb1[gi]) &
                             (wp > esc.gfw0[gi]) & ~tap)
        if idx.size == 0:
            continue
        a, b = esc.ga[gi], esc.gb[gi]
        ai = ap[idx, None]; bi = bp[idx, None]
        mm = ((ai >= esc.fa0[a:b]) & (ai <= esc.fa1[a:b]) & (bi >= esc.fb0[a:b]) & (bi <= esc.fb1[a:b]) &
              (wp[idx, None] > esc.fw0[a:b]))
        ri, pj = np.nonzero(mm)
        if ri.size == 0:
            continue
        pk = a + pj; ro = idx[ri]; M = esc.M[pk]
        ol = np.einsum("pij,pj->pi", M, P[ro] - esc.C[pk]); dl = np.einsum("pij,pj->pi", M, D[ro])
        if esc.gtipo[gi] == ELI:
            qa = (dl * dl).sum(1); qb = (ol * dl).sum(1)
            disc = qb * qb - qa * ((ol * ol).sum(1) - 1.0)
            tt = np.where(disc > 0, (-qb - np.sqrt(np.maximum(disc, 0.0))) / qa, np.inf)
        else:
            inv = 1.0 / np.where(np.abs(dl) < 1e-12, 1e-12, dl)
            t0 = (-1.0 - ol) * inv; t1 = (1.0 - ol) * inv
            tn = np.minimum(t0, t1).max(1); tf = np.maximum(t0, t1).min(1)
            tt = np.where(tf >= tn, tn, np.inf)
        tap[ro[(tt > EPS) & (tt < lim[ro])]] = True
    return tap


def normales(esc, pid, P):
    M = esc.M[pid]
    pl = np.einsum("nij,nj->ni", M, P - esc.C[pid])
    nl = pl.copy()
    caja = esc.tipo[pid] == CAJA
    if caja.any():
        q = pl[caja]; k = np.abs(q).argmax(1)
        z = np.zeros_like(q); z[np.arange(len(q)), k] = np.sign(q[np.arange(len(q)), k])
        nl[caja] = z
    nw = np.einsum("nji,nj->ni", M, nl)
    nw /= np.linalg.norm(nw, axis=1, keepdims=True)
    k = np.flatnonzero(esc.patron[pid] == 2)                       # relieve rugoso de las rocas
    if k.size:
        x, y, z = P[k, 0], P[k, 1], P[k, 2]
        nw[k, 0] += 0.26 * np.sin(8.3 * y + 3.1 * np.sin(6.2 * z)) + 0.13 * np.sin(21 * z + 17 * y)
        nw[k, 1] += 0.26 * np.sin(7.7 * z + 2.7 * np.sin(5.4 * x)) + 0.13 * np.sin(19 * x + 23 * z)
        nw[k, 2] += 0.26 * np.sin(9.1 * x + 3.3 * np.sin(6.8 * y)) + 0.13 * np.sin(22 * y + 18 * x)
        nw[k] /= np.linalg.norm(nw[k], axis=1, keepdims=True)
    return nw, pl


_BLANCO = np.array([0.96, 0.96, 0.93], f32)


def patrones(esc, pid, pl, P, alb, N):
    """Variaciones de color procedurales: bandas y lomo de los peces, moteado de roca, surcos y degradados."""
    pat = esc.patron[pid]
    if not pat.any():
        return
    for k in (np.flatnonzero((pat == 1) | (pat == 5)),):           # lomo oscuro y vientre claro
        if k.size:
            alb[k] *= (0.62 + 0.45 * suave(0.55 - 0.7 * pl[k, 2])).astype(f32)[:, None]
    k = np.flatnonzero(pat == 1)                                   # bandas blancas
    if k.size:
        b = suave((np.abs(np.sin(pl[k, 0] * 4.2 + 0.6)) - 0.80) / 0.10).astype(f32)[:, None]
        alb[k] = alb[k] * (1 - b) + _BLANCO * b
    k = np.flatnonzero(pat == 2)                                   # moteado de roca
    if k.size:
        x, y, z = (P[k, j].astype(f32) for j in range(3))
        m = (0.82 + 0.13 * np.sin(7.1 * x + 3.3 * np.sin(5.2 * z)) * np.sin(6.3 * y + 2.9 * np.sin(4.1 * x))
             + 0.06 * np.sin(23 * x + 17 * y + 29 * z))
        m *= 0.72 + 0.28 * suave((z - Z_FONDO) / 0.5)              # más oscura donde toca la arena
        verdin = (0.55 * suave((N[k, 2] - 0.25) / 0.6) * (0.6 + 0.4 * np.sin(3.1 * x + 2.3 * np.sin(2.7 * y)))).astype(f32)[:, None]
        alb[k] = alb[k] * m[:, None] * (1.0 - verdin) + verdin * m[:, None] * np.array([0.30, 0.42, 0.22], f32)
    k = np.flatnonzero(pat == 3)                                   # surcos de coral
    if k.size:
        q = pl[k].astype(f32)
        alb[k] *= (0.74 + 0.26 * np.sin(26 * q[:, 0] + 5 * np.sin(11 * q[:, 1]) + 9 * q[:, 2]))[:, None]
    k = np.flatnonzero(pat == 4)                                   # hojas: más claras hacia la punta
    if k.size:
        alb[k] *= (0.78 + 0.22 * pl[k, 0]).astype(f32)[:, None]


_ARENA = np.array([0.70, 0.63, 0.46], f32)


def arena(P, t, Dz, nivel):
    """Fondo de arena: tono variable y rizaduras (relieve por normal) que se atenúan con la distancia."""
    x = P[:, 0].astype(f32); y = P[:, 1].astype(f32)
    v = 0.86 + 0.09 * np.sin(0.7 * x + 1.3 * np.sin(0.5 * y)) + 0.06 * np.sin(1.1 * y + 0.9 * np.sin(0.8 * x + 1.0))
    N = np.zeros((len(x), 3)); N[:, 2] = 1.0
    if nivel == 0:
        pie = t / FOCAL / np.maximum(np.abs(Dz), 0.05)             # tamaño del píxel sobre el fondo (m)
        ate = np.clip(1.5 - pie / 0.03, 0.0, 1.0)
        if ate.any():
            fase = 17.0 * (0.94 * x + 0.34 * y) + 2.2 * np.sin(0.9 * y + 0.4 * x) + 1.3 * np.sin(1.7 * x - 0.6 * y + 1.0)
            s = 0.33 * ate * np.cos(fase)
            N[:, 0] = -0.94 * s; N[:, 1] = -0.34 * s
            N /= np.linalg.norm(N, axis=1, keepdims=True)
    return N, v[:, None] * _ARENA


_ONDAS = [(0.9, 0.4, 5, 0.0, 0.016), (-0.5, 1.3, 8, 1.7, 0.013), (1.9, -1.1, 11, 0.6, 0.009), (3.1, 2.2, 14, 2.2, 0.0045)]


def normal_superficie(P, t):
    """Normal (hacia abajo) de la superficie con un oleaje suave y periódico en el bucle."""
    sx = np.zeros(len(P)); sy = np.zeros(len(P))
    for kx, ky, m, f0, a in _ONDAS:
        c = a * np.cos(kx * P[:, 0] + ky * P[:, 1] + 2 * math.pi * m * t / T_ANIM + f0)
        sx += kx * c; sy += ky * c
    nrm = np.column_stack([sx, sy, -np.ones(len(P))])
    return nrm / np.linalg.norm(nrm, axis=1, keepdims=True)


def causticas(P, t):
    """Red de cáusticas del sol, proyectada a lo largo de su dirección y periódica en el bucle."""
    z = P[:, 2]
    u = (P[:, 0] - z * KX).astype(f32); v = (P[:, 1] - z * KY).astype(f32)
    w = f32(2 * math.pi * t / T_ANIM)
    qx = u + 0.42 * np.sin(0.9 * v + 3 * w) + 0.20 * np.sin(2.3 * u + 1.3 - 5 * w) + 0.08 * np.sin(5.1 * v + 0.7 + 7 * w)
    qy = v + 0.42 * np.sin(1.1 * u + 1.7 + 4 * w) + 0.20 * np.sin(2.0 * v + 0.4 + 6 * w) + 0.08 * np.sin(4.7 * u + 2.9 - 8 * w)
    r = 0.0
    for kx, ky, m, f0 in ((2.6, 1.2, 7, 0.0), (-1.3, 2.9, -5, 1.0), (1.8, -2.5, 6, 2.1)):
        a = 1.0 - np.abs(np.sin(kx * qx + ky * qy + m * w + f0))
        a *= a; a *= a
        r = r + a * a
    r *= 0.55 + 0.45 * np.sin(0.8 * u + 1.3 * np.sin(0.6 * v + 2 * w) + 0.5) * np.sin(0.7 * v - 3 * w)
    return 0.86 + np.clip(1.25 * r, 0.0, 1.5)


def sombrear(esc, O, D, t, pid, nivel, info=None):
    """Color de cada rayo dado su corte. nivel 0 = rayo primario (admite UN rebote de reflexión).
    Si se pasa info (dict), deja en info["sol"] qué puntos reciben sol directo."""
    n = len(t)
    P = O + t[:, None] * D
    col = np.zeros((n, 3), f32)
    N = np.zeros((n, 3))
    op = np.flatnonzero(pid != -2)
    if op.size:
        q = pid[op]; Pq = P[op]; Dq = D[op]
        Nq = np.zeros((op.size, 3)); alb = np.empty((op.size, 3), f32)
        esp = np.zeros(op.size, f32); tr = np.zeros(op.size, f32)
        su = np.flatnonzero(q == -3); pr = np.flatnonzero(q >= 0)
        if su.size:
            Nq[su], alb[su] = arena(Pq[su], t[op[su]], Dq[su, 2], nivel)
        if pr.size:
            qi = q[pr]
            Nq[pr], pl = normales(esc, qi, Pq[pr])
            a = esc.alb[qi]
            patrones(esc, qi, pl, Pq[pr], a, Nq[pr])
            alb[pr] = a; esp[pr] = esc.esp[qi]; tr[pr] = esc.trans[qi]
        Nq[(Nq * Dq).sum(1) > 0] *= -1.0
        Po = Pq + Nq * DESP
        prof = np.maximum(-Pq[:, 2], 0.0).astype(f32)[:, None]
        # sol direccional con sombra dura; la luz llega atenuada por el agua y modulada por las cáusticas
        ndl = (Nq @ SOL).astype(f32)
        dif = np.clip(ndl, 0.0, None)
        dif = dif * (1.0 - tr) + tr * (0.30 + 0.70 * np.abs(ndl))  # hojas y aletas dejan pasar la luz
        vis = np.zeros(op.size, f32)
        lit = np.flatnonzero(dif > 0)
        if lit.size:
            vis[lit] = ~sombra_sol(esc, Po[lit], q[lit])
        atsol = np.exp(-SIGMA * prof * f32(1.0 / SOL[2]))
        luz = SOL_COL * atsol * (dif * vis * causticas(Pq, esc.t))[:, None]
        luz += AMB_COL * np.exp(-SIGMA * prof * f32(0.7)) * (0.60 + 0.40 * Nq[:, 2].astype(f32))[:, None]
        # foco del morro (cono, con sombra)
        v = esc.foco_pos - Po; dist = np.linalg.norm(v, axis=1); vd = v / dist[:, None]
        cono = suave((-(vd @ esc.foco_dir) - FOCO_COS_EXT) / (FOCO_COS_INT - FOCO_COS_EXT))
        wf = (cono * np.clip((Nq * vd).sum(1), 0.0, None) / (1.0 + 0.15 * dist * dist) * suave((6.5 - dist) / 2.0)).astype(f32)
        k = np.flatnonzero(wf > 4e-3)
        if k.size:
            wf[k] *= ~sombra_luz(esc, Po[k])
            luz[k] += FOCO_COL * wf[k, None] * np.exp(-SIGMA * dist[k, None].astype(f32))
        c = alb * luz
        if pr.size:
            c[pr] += esc.emi[q[pr]]
        ke = np.flatnonzero((esp > 0) & (vis > 0))                 # brillo especular del sol
        if ke.size:
            hv = SOL - Dq[ke]; hv /= np.linalg.norm(hv, axis=1, keepdims=True)
            s = (np.clip((Nq[ke] * hv).sum(1), 0.0, None) ** 60).astype(f32)
            c[ke] += (esp[ke] * s)[:, None] * SOL_COL * atsol[ke] * f32(0.5)
        col[op] = c; N[op] = Nq
        if info is not None:
            info["sol"] = np.zeros(n, bool); info["sol"][op] = vis > 0
    elif info is not None:
        info["sol"] = np.zeros(n, bool)

    if nivel == 0:                                                 # un rebote: metal del dron y burbujas
        k = np.flatnonzero(pid >= 0)
        if k.size:
            k = k[esc.refl[pid[k]] > 0]
        if k.size:
            Nk = N[k]; cosv = -(D[k] * Nk).sum(1)
            R = D[k] + 2.0 * cosv[:, None] * Nk
            Ok = P[k] + Nk * DESP
            t2, p2 = intersecar(esc, Ok, R)
            cr = sombrear(esc, Ok, R, t2, p2, 1)
            kr = esc.refl[pid[k]]
            fr = (kr + (1.0 - kr) * (1.0 - np.clip(cosv, 0.0, 1.0).astype(f32)) ** 5)[:, None]
            col[k] = col[k] * (1.0 - fr) + fr * (0.45 + 0.55 * esc.alb[pid[k]]) * cr

    sup = np.flatnonzero(pid == -2)                                # superficie vista desde abajo
    if sup.size:
        Ps = P[sup]; Ds = D[sup]
        Ns = normal_superficie(Ps, esc.t)
        dn = (Ds * Ns).sum(1)
        cosi = np.clip(-dn, 0.0, 1.0).astype(f32)
        F = (1.0 - suave((cosi - 0.661) / 0.14))[:, None]          # 1 = reflexión total (fuera de la ventana)
        cielo = CIELO * (0.55 + 0.45 * cosi)[:, None]
        if nivel == 0:
            R = Ds - 2.0 * dn[:, None] * Ns
            R[:, 2] = np.minimum(R[:, 2], -0.03); R /= np.linalg.norm(R, axis=1, keepdims=True)
            Os = Ps + np.array([0, 0, -DESP])
            t2, p2 = intersecar(esc, Os, R)
            cr = sombrear(esc, Os, R, t2, p2, 1)
        else:
            cr = AGUA_REFLEJO
        brillo = (causticas(Ps * np.array([2.2, 2.2, 0.0]), esc.t) - 0.86) * np.exp(-0.06 * t[sup]).astype(f32)
        col[sup] = F * cr * f32(0.92) + (1.0 - F) * cielo + brillo[:, None] * AGUA_SOMERA * f32(0.55)

    # Beer-Lambert: extinción por distancia recorrida y luz difusa del agua (más oscura con la profundidad)
    tt = np.minimum(t, 200.0).astype(f32)[:, None]
    zmed = np.minimum(O[:, 2] + 0.5 * np.minimum(t, 14.0) * D[:, 2], 0.0).astype(f32)[:, None]
    agua = AGUA_HONDA + (AGUA_SOMERA - AGUA_HONDA) * np.exp(f32(0.20) * zmed)
    T = np.exp(-SIGMA * tt)
    return col * T + agua * (1.0 - T)


_AA = ((-0.125, -0.375), (0.375, -0.125), (-0.375, 0.125), (0.125, 0.375))   # submuestras extra (rejilla girada)
_VEC9 = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]


def _visibilidad_aa(esc, pos, D, cand):
    """Corte más cercano de rayos subpíxel: solo se prueban las primitivas vistas en los 3x3 píxeles vecinos."""
    m = len(D)
    dz = np.where(np.abs(D[:, 2]) < 1e-9, 1e-9, D[:, 2])
    t = np.where(dz > 0, -pos[2] / dz, (Z_FONDO - pos[2]) / dz)
    pid = np.where(dz > 0, -2, -3).astype(np.int32)
    ri, cj = np.nonzero(cand >= 0)
    if ri.size:
        pk = cand[ri, cj]
        ol = esc.Mo[pk]; dl = np.einsum("pij,pj->pi", esc.M[pk], D[ri])
        qa = (dl * dl).sum(1); qb = (ol * dl).sum(1)
        disc = qb * qb - qa * ((ol * ol).sum(1) - 1.0)
        tt = np.where(disc > 0, (-qb - np.sqrt(np.maximum(disc, 0.0))) / qa, np.inf)
        caja = np.flatnonzero(esc.tipo[pk] == CAJA)
        if caja.size:
            o = ol[caja]; d = dl[caja]
            inv = 1.0 / np.where(np.abs(d) < 1e-12, 1e-12, d)
            t0 = (-1.0 - o) * inv; t1 = (1.0 - o) * inv
            tn = np.minimum(t0, t1).max(1); tf = np.maximum(t0, t1).min(1)
            tt[caja] = np.where(tf >= tn, tn, np.inf)
        tt[tt < 1e-6] = np.inf
        Tm = np.full(cand.shape, np.inf); Tm[ri, cj] = tt
        j = Tm.argmin(1); fil = np.arange(m); tm = Tm[fil, j]
        mej = tm < t
        t[mej] = tm[mej]; pid[mej] = cand[fil, j][mej]
    return t, pid


def colorear(esc, cam, tbuf, idbuf, info=None):
    """Sombrea por teselas. En los píxeles de borde (cambio de objeto o límite de sombra) se trazan
    4 rayos más por píxel y se promedia: antialiasing solo donde hace falta."""
    pos, f, r, u = cam
    borde = np.zeros((H, W), bool)
    d = idbuf[:, 1:] != idbuf[:, :-1]; borde[:, 1:] |= d; borde[:, :-1] |= d
    d = idbuf[1:, :] != idbuf[:-1, :]; borde[1:, :] |= d; borde[:-1, :] |= d
    col = np.empty((H, W, 3), f32)
    nborde = 0
    for ya in range(0, H, TESELA):
        yb = min(ya + TESELA, H)
        for xa in range(0, W, TESELA):
            xb = min(xa + TESELA, W); ny, nx = yb - ya, xb - xa
            D = (f + XS[None, xa:xb, None] * r + YS[ya:yb, None, None] * u).reshape(-1, 3)
            nrm = np.linalg.norm(D, axis=1)
            D /= nrm[:, None]
            inf = {}
            c = sombrear(esc, np.broadcast_to(pos, D.shape), D, tbuf[ya:yb, xa:xb].ravel() * nrm,
                         idbuf[ya:yb, xa:xb].ravel(), 0, inf)
            b = borde[ya:yb, xa:xb].copy(); v = inf["sol"].reshape(ny, nx)
            d = v[:, 1:] != v[:, :-1]; b[:, 1:] |= d; b[:, :-1] |= d
            d = v[1:, :] != v[:-1, :]; b[1:, :] |= d; b[:-1, :] |= d
            idx = np.flatnonzero(b.ravel())
            if idx.size:
                nborde += idx.size
                acc = c[idx].copy()
                gy = ya + idx // nx; gx = xa + idx % nx
                X = XS[gx]; Y = YS[gy]
                cand = np.stack([idbuf[np.clip(gy + dy, 0, H - 1), np.clip(gx + dx, 0, W - 1)] for dy, dx in _VEC9], 1)
                cand.sort(axis=1)
                cand[:, 1:][cand[:, 1:] == cand[:, :-1]] = -9          # sin repetidos
                for ox, oy in _AA:
                    D2 = f + (X + ox / FOCAL)[:, None] * r + (Y - oy / FOCAL)[:, None] * u
                    D2 /= np.linalg.norm(D2, axis=1, keepdims=True)
                    O2 = np.broadcast_to(pos, D2.shape)
                    t2, p2 = _visibilidad_aa(esc, pos, D2, cand)
                    acc += sombrear(esc, O2, D2, t2, p2, 0)
                c[idx] = acc * f32(1.0 / (1 + len(_AA)))
            col[ya:yb, xa:xb] = c.reshape(ny, nx, 3)
    if info is not None:
        info["borde"] = nborde / float(W * H)
    return col


def _ampliar(a, tam):
    return np.asarray(Image.fromarray(a.astype(f32)).resize(tam, Image.BILINEAR))


def rayos_de_luz(cam, t):
    """Rayos de sol volumétricos: integral a lo largo de cada rayo de un patrón en el "espacio del sol".
    Se calcula a 1/4 de resolución (es muy suave) y se amplía."""
    pos, f, r, u = cam
    k = 4
    h, w = (H + k - 1) // k, (W + k - 1) // k
    xs = ((np.arange(w) + 0.5) * k - W / 2) / FOCAL; ys = (H / 2 - (np.arange(h) + 0.5) * k) / FOCAL
    dx = (f[0] + xs[None, :] * r[0] + ys[:, None] * u[0]).astype(f32)
    dy = (f[1] + xs[None, :] * r[1] + ys[:, None] * u[1]).astype(f32)
    dz = (f[2] + xs[None, :] * r[2] + ys[:, None] * u[2]).astype(f32)
    nrm = np.sqrt(dx * dx + dy * dy + dz * dz); dx /= nrm; dy /= nrm; dz /= nrm
    dzs = np.where(np.abs(dz) < 1e-6, f32(1e-6), dz)
    tfin = np.minimum(np.where(dzs > 0, (-pos[2] - Z_FONDO) / dzs, (Z_FONDO - pos[2]) / dzs), 26.0).astype(f32)
    w0 = f32(2 * math.pi * t / T_ANIM)
    acc = np.zeros((h, w), f32)
    ns = 18
    for j in range(ns):
        s = tfin * f32((j + 0.5) / ns)
        z = -np.abs(f32(pos[2]) + s * dz)                          # reflejado en la superficie si la cruza
        uu = f32(pos[0]) + s * dx - z * f32(KX); vv = f32(pos[1]) + s * dy - z * f32(KY)
        a = np.sin(0.85 * uu + 0.55 * np.sin(0.45 * vv + 2 * w0) + 3 * w0)
        b = np.sin(0.75 * vv - 0.50 * uu + 0.60 * np.sin(0.40 * uu - 3 * w0) + 1.0 - 2 * w0)
        p = (0.5 + 0.5 * a) * (0.5 + 0.5 * b)
        acc += p * p * p * np.exp(f32(0.075 / SOL[2]) * z - f32(0.05) * s)
    acc *= tfin / ns
    acc *= 0.55 + 0.45 * np.clip(dx * f32(SOL[0]) + dy * f32(SOL[1]) + dz * f32(SOL[2]), 0, 1) ** 2
    return _ampliar(acc, (W, H))


def haz_foco(col, esc, cam, tbuf):
    """Haz visible del foco: luz dispersada en el agua dentro del cono, a media resolución."""
    pos, f, r, u = cam
    Lp, eje, rango = esc.foco_pos, esc.foco_dir, esc.foco_rango
    rad = min(2.6, rango * 0.5 + 0.4)
    c0 = Lp + eje * min(rango * 0.5, 2.2)
    v = c0 - pos; zc = v @ f
    if zc < rad + 0.3:
        return
    px = W / 2 + FOCAL * (v @ r) / zc; py = H / 2 - FOCAL * (v @ u) / zc; rp = FOCAL * rad / (zc - rad)
    xa, xb = max(0, int(px - rp)), min(W, int(px + rp) + 1); ya, yb = max(0, int(py - rp)), min(H, int(py + rp) + 1)
    if xb - xa < 4 or yb - ya < 4:
        return
    X = XS[None, xa:xb:2]; Y = YS[ya:yb:2, None]
    dx = f[0] + X * r[0] + Y * u[0]; dy = f[1] + X * r[1] + Y * u[1]; dz = f[2] + X * r[2] + Y * u[2]
    nrm = np.sqrt(dx * dx + dy * dy + dz * dz); dx /= nrm; dy /= nrm; dz /= nrm
    tmax = tbuf[ya:yb:2, xa:xb:2] * nrm
    s0 = v[0] * dx + v[1] * dy + v[2] * dz
    acc = np.zeros(dx.shape); ns = 14; paso = 2 * rad / ns
    o = pos - Lp
    for j in range(ns):
        s = s0 + (j + 0.5 - ns / 2) * paso
        wx = o[0] + s * dx; wy = o[1] + s * dy; wz = o[2] + s * dz
        d = np.sqrt(wx * wx + wy * wy + wz * wz) + 1e-6
        ca = (wx * eje[0] + wy * eje[1] + wz * eje[2]) / d
        acc += (suave((ca - HAZ_COS_EXT) / (HAZ_COS_INT - HAZ_COS_EXT)) / (1.0 + 2.0 * d * d) * np.exp(-0.25 * d)
                * suave((rango - d) / 0.35) * ((s > 0.05) & (s < tmax)))
    acc *= paso
    col[ya:yb, xa:xb] += _ampliar(acc, (xb - xa, yb - ya))[..., None] * HAZ_COL


_PART_COL = np.array([0.72, 0.95, 1.00], f32)


def particulas(col, cam, tbuf, t):
    """Partículas en suspensión: motas tenues con deriva periódica, ocultas tras la geometría."""
    pos, f, r, u = cam
    p = _P0 + _PAMP * np.sin(_PFRE * (2 * math.pi * t / T_ANIM) + _PFAS)
    v = p - pos; zc = v @ f
    ok = zc > 0.6
    zs = np.where(ok, zc, 1.0)
    x = W / 2 + FOCAL * (v @ r) / zs; y = H / 2 - FOCAL * (v @ u) / zs
    ok &= (x > 3) & (x < W - 4) & (y > 3) & (y < H - 4)
    esc_px = W / 3200.0
    for k in np.flatnonzero(ok):
        ix, iy = int(x[k]), int(y[k])
        d = float(np.linalg.norm(v[k]))
        if d > tbuf[iy, ix] * math.sqrt(1.0 + XS[ix] ** 2 + YS[iy] ** 2):
            continue
        rho = min(max(FOCAL * _PTAM[k] / zc[k], 0.9 * esc_px), 4.5 * esc_px)
        b = _PBRI[k] * 0.55 * math.exp(-0.10 * d) * math.exp(0.05 * p[k, 2]) * min(1.0, (d - 0.6) / 1.2)
        m = int(math.ceil(2.2 * rho))
        ax = np.arange(ix - m, ix + m + 1); ay = np.arange(iy - m, iy + m + 1)
        okx = (ax >= 0) & (ax < W); oky = (ay >= 0) & (ay < H)
        g = np.exp(-((ay[oky, None] - y[k]) ** 2 + (ax[None, okx] - x[k]) ** 2) / (2 * (0.55 * rho) ** 2)).astype(f32)
        col[ay[oky][0]:ay[oky][-1] + 1, ax[okx][0]:ax[okx][-1] + 1] += (b * g)[..., None] * _PART_COL


# ----------------------------------------------------------------------------------
# DIBUJO EN ESPACIO DE PANTALLA (etiquetas del poste y HUD) con Pillow, a resolución nativa
# ----------------------------------------------------------------------------------
def fuente(tam, negrita=False, mono=False):
    nombres = (["DejaVuSansMono-Bold.ttf", "consolab.ttf", "courbd.ttf"] if mono else []) + \
              (["DejaVuSans-Bold.ttf", "arialbd.ttf"] if negrita else []) + ["DejaVuSans.ttf", "arial.ttf"]
    for nm in nombres:
        for ruta in (nm, "/usr/share/fonts/truetype/dejavu/" + nm):
            try:
                return ImageFont.truetype(ruta, tam)
            except OSError:
                pass
    try:
        return ImageFont.load_default(tam)
    except TypeError:
        return ImageFont.load_default()


S_HUD = W / 700.0
F_PROG = fuente(max(7, round(11 * S_HUD)), True, True)
F_TXT = fuente(max(7, round(11 * S_HUD)))
F_VAL = fuente(max(7, round(11 * S_HUD)), True)
F_ETI = fuente(max(7, round(10 * S_HUD)), True)

SEG = {"0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg", "6": "acdefg",
       "7": "abc", "8": "abcdefg", "9": "abcdfg", "A": "abcefg", "B": "cdefg", "C": "adef", "d": "bcdeg",
       "D": "bcdeg", "E": "adefg", "F": "aefg", None: ""}
_ON, _OFF = (255, 84, 62, 255), (70, 26, 30, 255)


def _display(d, x, y, car, dp, s):
    w, h, g = 20 * s, 38 * s, 4 * s
    rect = {"a": (g * .7, 0, w - g * .7, g), "b": (w - g, g * .6, w, h / 2 - g * .2),
            "c": (w - g, h / 2 + g * .2, w, h - g * .6), "d": (g * .7, h - g, w - g * .7, h),
            "e": (0, h / 2 + g * .2, g, h - g * .6), "f": (0, g * .6, g, h / 2 - g * .2),
            "g": (g * .7, h / 2 - g / 2, w - g * .7, h / 2 + g / 2)}
    for k, (x0, y0, x1, y1) in rect.items():
        d.rounded_rectangle([x + x0, y + y0, x + x1, y + y1], radius=g / 2, fill=_ON if k in SEG[car] else _OFF)
    r = 2.6 * s
    d.ellipse([x + w + 4 * s - r, y + h - 2 * r, x + w + 4 * s + r, y + h], fill=_ON if dp else _OFF)


def dibujar_etiquetas(img, cam, tbuf):
    d = ImageDraw.Draw(img, "RGBA"); s = S_HUD
    for m in MARCAS:
        x, y, z = proyectar(cam, [POSTE[0] + 0.17, POSTE[1], -max(m, 0.04)])
        if z < 0.5 or not (0 <= x < W - 1 and 0 <= y < H - 1) or (x < 275 * s and y < 122 * s):
            continue
        dist = float(np.linalg.norm(np.array([POSTE[0] + 0.17, POSTE[1], -max(m, 0.04)]) - cam[0]))
        ix, iy = int(x), int(y)
        if tbuf[iy, ix] * math.sqrt(1.0 + XS[ix] ** 2 + YS[iy] ** 2) < dist - 0.35:       # tapada por algo más cercano
            continue
        txt = ("%g m" % m).replace(".", ",")
        d.text((x + 7 * s + 0.6 * s, y - 6 * s + 0.6 * s), txt, font=F_ETI, fill=(0, 20, 30, 190))
        d.text((x + 7 * s, y - 6 * s), txt, font=F_ETI, fill=(255, 255, 255, 255))


def dibujar_hud(img, e):
    """Programa, displays de 7 segmentos e indicadores. Mismas reglas que en las versiones anteriores."""
    s = S_HUD
    d = ImageDraw.Draw(img, "RGBA")
    f = e["fase"]
    x0, y0 = 8 * s, 8 * s
    d.rounded_rectangle([x0, y0, x0 + 262 * s, y0 + 107 * s], radius=7 * s, fill=(8, 20, 34, 218),
                        outline=(150, 190, 210, 255), width=max(1, round(0.7 * s)))
    for k, ins in enumerate(RECORRIDOS[f["rec"]]["programa"]):      # programa, con la instrucción en curso
        cx = x0 + 8 * s + k * 35 * s
        act = f["chip"] == k
        if act:
            d.rounded_rectangle([cx, y0 + 5 * s, cx + 31 * s, y0 + 21 * s], radius=4 * s, fill=(245, 179, 1, 255))
        d.text((cx + 15.5 * s, y0 + 13 * s), ins, font=F_PROG, anchor="mm",
               fill=(10, 20, 30, 255) if act else (200, 215, 225, 255))
    d.rounded_rectangle([x0 + 8 * s, y0 + 29 * s, x0 + 80 * s, y0 + 87 * s], radius=4 * s, fill=(16, 8, 12, 255))
    _display(d, x0 + 15 * s, y0 + 39 * s - 5 * s, e["d1"], e["dp"], s)
    _display(d, x0 + 47 * s, y0 + 39 * s - 5 * s, e["d2"], e["dp"], s)
    if f["tipo"] == "reposo":
        v0, v1, vg, vs = "0% parado", "0% parado", "00 parado", "0 baja"
    elif f["tipo"] == "vertical":
        v0, v1, vg = ("70% sube" if f["sb"] else "70% baja"), "0% parado", "00 parado"
        vs = "1 sube" if f["sb"] else "0 baja"
    else:
        v0, vs = "30% mantiene", "0 baja"
        v1 = "50% avanza" if f["tipo"] == "avance" else "50% gira"
        vg = {"11": "11 avanza", "10": "10 gira dcha", "01": "01 gira izda"}[f["ga"]]
    filas = [("PWM0", v0), ("PWM1", v1), ("Giro_Avance", vg), ("Subir_Bajar", vs), ("Marcha/Paro", None)]
    for k, (et, val) in enumerate(filas):
        yy = y0 + 28 * s + k * 14.6 * s
        d.text((x0 + 90 * s, yy), et, font=F_TXT, fill=(170, 195, 210, 255))
        if val is not None:
            d.text((x0 + 164 * s, yy), val, font=F_VAL, fill=(255, 255, 255, 255))
        else:                                                      # botón: se ilumina mientras está pulsado
            caja = [x0 + 164 * s, yy + 1.5 * s, x0 + 200 * s, yy + 12.5 * s]
            if e["boton"]:
                d.rounded_rectangle(caja, radius=5.5 * s, fill=(245, 179, 1, 255))
            else:
                d.rounded_rectangle(caja, radius=5.5 * s, fill=(28, 44, 60, 255), outline=(120, 150, 170, 255),
                                    width=max(1, round(0.6 * s)))


# ----------------------------------------------------------------------------------
# RENDER DE UN FOTOGRAMA
# ----------------------------------------------------------------------------------
_VIN = (1.0 - 0.34 * (((np.arange(W) + 0.5) / W - 0.5)[None, :] ** 2 + ((np.arange(H) + 0.5) / H - 0.5)[:, None] ** 2)).astype(f32)


def revelar(col):
    """Exposición, gamma, contraste suave y viñeta. Devuelve la imagen de 8 bits."""
    np.clip(col, 0.0, None, out=col)
    col *= -EXPOSICION
    np.exp(col, out=col)
    np.subtract(1.0, col, out=col)
    np.power(col, f32(1 / 2.2), out=col)
    col *= (0.52 + 0.48 * col * (3.0 - 2.0 * col)) * _VIN[..., None]
    return Image.fromarray((np.clip(col, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8))


def render_fotograma(i, tiempos=None):
    t = i / FPS
    marcas = [time.time()]
    esc = escena(i); cam = camara(i); marcas.append(time.time())
    tbuf, idbuf = visibilidad(esc, cam); marcas.append(time.time())
    inf = {}
    col = colorear(esc, cam, tbuf, idbuf, inf); marcas.append(time.time())
    col += rayos_de_luz(cam, t)[..., None] * RAYOS_COL
    haz_foco(col, esc, cam, tbuf)
    particulas(col, cam, tbuf, t); marcas.append(time.time())
    img = revelar(col)
    img = Image.blend(img, img.filter(ImageFilter.GaussianBlur(0.6)), 0.3)      # suavizado leve final
    dibujar_etiquetas(img, cam, tbuf)
    dibujar_hud(img, EST[i]); marcas.append(time.time())
    if tiempos is not None:
        tiempos.append(dict(zip(("escena", "visibilidad", "sombreado", "volumen", "final"), np.diff(marcas))))
        tiempos[-1]["borde_%"] = 100 * inf["borde"]
    return img


# ----------------------------------------------------------------------------------
# COMPROBACIONES PREVIAS
# ----------------------------------------------------------------------------------
_CASCO = np.array([[0.33, 0, 0], [-0.45, 0, 0], [0, 0.42, 0.06], [0, -0.42, 0.06], [0.105, 0.31, 0.06],
                   [-0.105, 0.31, 0.06], [0.105, -0.31, 0.06], [-0.105, -0.31, 0.06], [0, 0, 0.14], [0, 0, -0.13],
                   [-0.37, 0.18, 0], [-0.37, -0.18, 0], [0.2, 0.15, 0], [0.2, -0.15, 0], [-0.3, 0, 0.17]])


def comprobar(paso=1):
    """Encuadre, visibilidad y colisiones del dron, lectura del sónar en cada fase y cierre del bucle."""
    info = dict(x=[1, 0], y=[1, 0], tapado=[], choque=[], camara=9.0, frente={})
    for i in range(0, NF, paso):
        esc = escena(i, con_estela=False); cam = camara(i); e = EST[i]; f = e["fase"]
        x, y, z = proyectar(cam, POS[i])
        info["x"] = [min(info["x"][0], x / W), max(info["x"][1], x / W)]
        info["y"] = [min(info["y"][0], y / H), max(info["y"][1], y / H)]
        d = POS[i] - cam[0]; dist = np.linalg.norm(d)
        _, pid = intersecar(esc, cam[0][None], (d / dist)[None])
        a, b = esc.dron
        if not (a <= pid[0] < b):
            info["tapado"].append(i)
        Rz = rot_z(-math.radians(e["rumbo"]))
        pts = POS[i] + _CASCO @ Rz.T
        otros = np.ones(len(esc.C), bool); otros[a:b] = False
        pl = np.einsum("kij,nkj->nki", esc.M[otros], pts[:, None, :] - esc.C[None, otros])
        dentro = np.where(esc.tipo[otros] == ELI, (pl * pl).sum(-1) < 1.0, np.abs(pl).max(-1) < 1.0)
        if dentro.any():
            info["choque"].append(i)
        info["camara"] = min(info["camara"], float((np.linalg.norm(esc.C - cam[0], axis=1)
                                                    - np.linalg.norm(np.linalg.inv(esc.M), axis=(1, 2))).min()))
        fr = info["frente"].setdefault(f["n"], [99.0, 99.0])
        fr[0] = min(fr[0], esc.frente); fr[1] = esc.frente             # mínimo y última lectura de la fase
    return info


def informe_comprobaciones(info):
    print("Dron en pantalla: x %.2f-%.2f, y %.2f-%.2f (fracción del cuadro)" % tuple(info["x"] + info["y"]))
    print("Fotogramas con el dron tapado: %d; con colisión: %d; holgura mínima de la cámara: %.2f m"
          % (len(info["tapado"]), len(info["choque"]), info["camara"]))
    ok = not info["choque"] and info["camara"] > 0.3 and 0.06 < info["x"][0] and info["x"][1] < 0.94 \
        and 0.30 < info["y"][0] and info["y"][1] < 0.92
    for f in FASES:
        if f["n"] not in info["frente"] or f["tipo"] not in ("avance", "giro"):
            continue
        mn, ult = info["frente"][f["n"]]
        txt = "  fase %2d %-9s %-3s sónar mín. %5.2f m" % (f["n"], f["nombre"], f["ins"], mn)
        if f["tipo"] == "avance":
            aborta = "aborta" in f["nota"]
            bien = (ult < UMBRAL_OBST + 0.06 and mn > 0.15) if aborta else mn > UMBRAL_OBST
            txt += "  (aborta al llegar a 0,2 m)" if aborta else "  (libre)"
        else:
            ignora = "ignora" in f["nota"]
            bien = mn < UMBRAL_OBST if ignora else True
            txt += "  (obstáculo ignorado)" if ignora else ""
        print(txt + ("" if bien else "   <-- REVISAR"))
        ok &= bien
    print("Comprobaciones:", "CORRECTAS" if ok else "CON AVISOS")
    return ok


# ----------------------------------------------------------------------------------
# RENDER POR LOTES Y MONTAJE
# ----------------------------------------------------------------------------------
def _trabajo(args):
    i, carpeta = args
    ruta = os.path.join(carpeta, "f_%04d.png" % i)
    if os.path.exists(ruta):
        return i, 0.0
    t0 = time.time()
    img = render_fotograma(i)
    img.save(ruta + ".tmp", "PNG", compress_level=1)
    os.replace(ruta + ".tmp", ruta)
    return i, time.time() - t0


def render_lote(indices, carpeta, procesos):
    os.makedirs(carpeta, exist_ok=True)
    pend = [i for i in indices if not os.path.exists(os.path.join(carpeta, "f_%04d.png" % i))]
    print("Fotogramas pendientes: %d de %d (%dx%d, %d procesos)" % (len(pend), len(indices), W, H, procesos), flush=True)
    t0 = time.time(); hechos = 0
    with Pool(procesos) as pool:
        for i, dt in pool.imap_unordered(_trabajo, [(i, carpeta) for i in pend]):
            hechos += 1
            if hechos % 10 == 0 or hechos == len(pend):
                tr = time.time() - t0
                print("  %d/%d  (%.1f s/fotograma por proceso, quedan %.0f min)"
                      % (hechos, len(pend), dt, tr / hechos * (len(pend) - hechos) / 60), flush=True)


def _trozos_webp(datos):
    """Extrae de un WebP de un fotograma los trozos de imagen (VP8, VP8L o ALPH)."""
    assert datos[:4] == b"RIFF" and datos[8:12] == b"WEBP"
    pos, out = 12, b""
    while pos + 8 <= len(datos):
        tam = int.from_bytes(datos[pos + 4:pos + 8], "little"); fin = pos + 8 + tam + (tam & 1)
        if datos[pos:pos + 4] in (b"VP8 ", b"VP8L", b"ALPH"):
            out += datos[pos:fin] + (b"\0" if fin > len(datos) else b"")
        pos = fin
    return out


def _codifica_webp(args):
    ruta, tam, calidad, metodo = args
    im = Image.open(ruta).convert("RGB")
    if im.size != tuple(tam):
        im = im.resize(tam, Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "WEBP", quality=calidad, method=metodo)
    return _trozos_webp(b.getvalue())


def escribir_webp_animado(salida, trozos, w, h, duraciones):
    """Contenedor WebP animado (RIFF/VP8X/ANIM/ANMF) con un fotograma completo por trozo; bucle infinito."""
    u24 = lambda v: int(v).to_bytes(3, "little")
    u32 = lambda v: int(v).to_bytes(4, "little")
    cuerpo = [b"VP8X", u32(10), bytes([0x02, 0, 0, 0]), u24(w - 1), u24(h - 1), b"ANIM", u32(6), bytes(4), bytes(2)]
    for tz, dur in zip(trozos, duraciones):
        carga = u24(0) + u24(0) + u24(w - 1) + u24(h - 1) + u24(dur) + bytes([0x02]) + tz
        cuerpo += [b"ANMF", u32(len(carga)), carga, b"\0" * (len(carga) & 1)]
    cuerpo = b"".join(cuerpo)
    with open(salida, "wb") as fh:
        fh.write(b"RIFF" + u32(len(cuerpo) + 4) + b"WEBP" + cuerpo)


def montar_webp(rutas, salida, tam, objetivo_mb, procesos, metodo=6, calidades=(92, 88, 84, 80, 76, 72, 68, 64, 60, 55, 50)):
    """Elige la mayor calidad que cabe en objetivo_mb (estimada con una muestra) y monta la animación."""
    muestra = rutas[::max(1, len(rutas) // 24)]
    with Pool(procesos) as pool:
        elegida = calidades[-1]
        for q in calidades:
            est = sum(map(len, pool.map(_codifica_webp, [(r, tam, q, 4) for r in muestra]))) / len(muestra) * len(rutas) / 1e6
            if est <= objetivo_mb:
                elegida = q
                break
        trozos = pool.map(_codifica_webp, [(r, tam, elegida, metodo) for r in rutas], chunksize=4)
    n = len(rutas)
    dur = [int(round((k + 1) * 1000 * T_ANIM / n)) - int(round(k * 1000 * T_ANIM / n)) for k in range(n)]
    escribir_webp_animado(salida, trozos, tam[0], tam[1], dur)
    print("%s: %dx%d, %d fotogramas, calidad %d, %.1f MB" % (salida, tam[0], tam[1], n, elegida, os.path.getsize(salida) / 1e6))


def montar_mp4(carpeta, salida, objetivo_mb):
    ff = shutil.which("ffmpeg")
    if not ff:
        print("ffmpeg no encontrado: se omite el MP4")
        return
    kbps = int(objetivo_mb * 8000 / T_ANIM)
    base = [ff, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", os.path.join(carpeta, "f_%04d.png"),
            "-c:v", "libx264", "-preset", "slow", "-pix_fmt", "yuv420p", "-b:v", "%dk" % kbps,
            "-passlogfile", os.path.join(carpeta, "x264")]
    subprocess.run(base + ["-pass", "1", "-an", "-f", "null", os.devnull], check=True)
    subprocess.run(base + ["-pass", "2", "-movflags", "+faststart", salida], check=True)
    print("%s: %.1f MB" % (salida, os.path.getsize(salida) / 1e6))


def main():
    base = _arg("--salida", "dron")
    carpeta = _arg("--dir", "fotogramas_%d" % W)
    procesos = _arg("--procesos", cpu_count(), int)
    if "--tabla" in sys.argv:
        print(tabla_tiempos()); return
    if "--fotograma" in sys.argv:
        i = _arg("--fotograma", 0, int); tm = []
        t0 = time.time(); img = render_fotograma(i, tm)
        img.save(base if base.lower().endswith(".png") else base + ".png")
        print("Fotograma %d (%dx%d) en %.1f s: %s" % (i, W, H, time.time() - t0,
              ", ".join("%s %.2f" % kv for kv in tm[0].items()))); return
    indices = list(range(_arg("--desde", 0, int), _arg("--hasta", NF, int), _arg("--paso", 1, int)))
    solo = [a for a in ("--comprobar", "--render", "--montar") if a in sys.argv]
    if not solo or "--comprobar" in solo:
        print(tabla_tiempos() + "\n")
        informe_comprobaciones(comprobar())
    if not solo or "--render" in solo:
        t0 = time.time(); render_fotograma(NF // 2); dt = time.time() - t0     # render de PRUEBA
        print("Prueba: %.1f s por fotograma; estimación total %.0f min con %d procesos"
              % (dt, dt * len(indices) / procesos / 60, procesos), flush=True)
        render_lote(indices, carpeta, procesos)
    if not solo or "--montar" in solo:
        rutas = [os.path.join(carpeta, "f_%04d.png" % i) for i in indices]
        falta = [r for r in rutas if not os.path.exists(r)]
        assert not falta, "Faltan %d fotogramas (p. ej. %s)" % (len(falta), falta[0])
        lig = _arg("--ligera", 0.3, float)                         # versión ligera para el README: 960x540
        montar_webp(rutas, base + "_4k.webp", (W, H), _arg("--mb4k", 90.0, float), procesos, metodo=4)
        montar_webp(rutas, base + ".webp", (int(W * lig / 2) * 2, int(H * lig / 2) * 2), _arg("--mb", 20.0, float), procesos)
        if len(indices) == NF:
            montar_mp4(carpeta, base + "_4k.mp4", _arg("--mbmp4", 19.0, float))
        with open(base + "_tiempos.md", "w", encoding="utf-8") as fh:
            fh.write(tabla_tiempos() + "\n")


if __name__ == "__main__":
    main()
