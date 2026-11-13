| Recorrido | Fase | Inicio (sist.) | Fin (sist.) | Inicio (anim.) | Fin (anim.) | Instrucción | Rumbo | Profundidad | PWM0 | PWM1 | Giro_Avance | Subir_Bajar | Evento |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 Completo | 0 Espera | 0,00 s | 0,50 s | 0,00 s | 0,36 s | ninguna | 0° | 0,0 m | 0 % | 0 % | 00 | 0 |  |
| 1 Completo | 1 Descenso | 0,50 s | 3,50 s | 0,36 s | 2,50 s | C2 | 0° | 0,0 → 5,0 m | 70 % | 0 % | 00 | 0 |  |
| 1 Completo | 2 Avance | 3,50 s | 8,50 s | 2,50 s | 6,07 s | A5 | 0° | 5,0 m | 30 % | 50 % | 11 | 0 |  |
| 1 Completo | 3 Giro dcha | 8,50 s | 10,50 s | 6,07 s | 7,50 s | D2 | 0 → 90° | 5,0 m | 30 % | 50 % | 10 | 0 |  |
| 1 Completo | 4 Avance | 10,50 s | 13,50 s | 7,50 s | 9,64 s | A3 | 90° | 5,0 m | 30 % | 50 % | 11 | 0 |  |
| 1 Completo | 5 Giro izda | 13,50 s | 15,50 s | 9,64 s | 11,07 s | E2 | 90 → 0° | 5,0 m | 30 % | 50 % | 01 | 0 |  |
| 1 Completo | 6 Avance | 15,50 s | 19,50 s | 11,07 s | 13,93 s | A4 | 0° | 5,0 m | 30 % | 50 % | 11 | 0 |  |
| 1 Completo | 7 Ascenso | 19,50 s | 22,50 s | 13,93 s | 16,07 s | F0 | 0° | 5,0 → 0,0 m | 70 % | 0 % | 00 | 1 |  |
| 2 Obstáculos | 8 Reposo | 22,50 s | 23,50 s | 16,07 s | 16,79 s | ninguna | 0° | 0,0 m | 0 % | 0 % | 00 | 0 |  |
| 2 Obstáculos | 9 Descenso | 23,50 s | 26,50 s | 16,79 s | 18,93 s | C3 | 0° | 0,0 → 7,5 m | 70 % | 0 % | 00 | 0 |  |
| 2 Obstáculos | 10 Giro izda | 26,50 s | 30,50 s | 18,93 s | 21,79 s | E4 | 0 → -180° | 7,5 m | 30 % | 50 % | 01 | 0 | obstáculo a menos de 0,2 m durante el giro: se ignora |
| 2 Obstáculos | 11 Avance | 30,50 s | 36,50 s | 21,79 s | 26,07 s | A6 | -180° | 7,5 m | 30 % | 50 % | 11 | 0 |  |
| 2 Obstáculos | 12 Giro dcha | 36,50 s | 37,50 s | 26,07 s | 26,79 s | D1 | -180 → -135° | 7,5 m | 30 % | 50 % | 10 | 0 |  |
| 2 Obstáculos | 13 Avance | 37,50 s | 41,74 s | 26,79 s | 29,82 s | A8 | -135° | 7,5 m | 30 % | 50 % | 11 | 0 | obstáculo a menos de 0,2 m durante el avance: aborta |
| 2 Obstáculos | 14 Ascenso | 41,74 s | 44,74 s | 29,82 s | 31,96 s | ninguna | -135° | 7,5 → 0,0 m | 70 % | 0 % | 00 | 1 | aborto por obstáculo |
| 3 Paro | 15 Reposo | 44,74 s | 45,74 s | 31,96 s | 32,67 s | ninguna | -135° | 0,0 m | 0 % | 0 % | 00 | 0 |  |
| 3 Paro | 16 Descenso | 45,74 s | 48,74 s | 32,67 s | 34,82 s | C1 | -135° | 0,0 → 2,5 m | 70 % | 0 % | 00 | 0 |  |
| 3 Paro | 17 Giro dcha | 48,74 s | 51,74 s | 34,82 s | 36,96 s | D5 | -135 → 0° | 2,5 m | 30 % | 50 % | 10 | 0 | se pulsa Marcha/Paro durante el giro |
| 3 Paro | 18 Ascenso | 51,74 s | 54,74 s | 36,96 s | 39,10 s | ninguna | 0° | 2,5 → 0,0 m | 70 % | 0 % | 00 | 1 | paro con el botón |
| 1 Completo | 19 Reposo | 54,74 s | 56,00 s | 39,10 s | 40,00 s | ninguna | 0° | 0,0 m | 0 % | 0 % | 00 | 0 |  |

Bucle de 40 s de animación = 56 s de sistema (1 s de animación = 1,4 s de sistema), 600 fotogramas a 15 fps.
Rumbo: 0° = dirección inicial (+X); positivo = giro horario visto desde arriba.
Marcha/Paro se pulsa en los instantes (s de sistema): 0,50, 23,50, 45,74, 51,74.
Parámetros de animación (NO vienen en el enunciado): avance 0,5 m/s, giro 45 °/s, descenso y ascenso de 3 s con interpolación suave, esperas entre recorridos y escala de tiempo x1,4. Con el paro se supone que el dron sube.
