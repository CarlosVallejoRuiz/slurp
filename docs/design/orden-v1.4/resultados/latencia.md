# v1.4 — latencia de la implementación (2026-11-27)

**Dónde está y qué se midió.**

- La implementación está en la rama `v1.4-orden` (`6be8f4f`), **sin fusionar**.
- `score_nodes` da exactamente los scores del candidato validado: recall, tokens y puesto del
  primer relevante iguales en las 1.556 preguntas (las 1.525 del banco y las 31 etiquetadas).

## Primera consulta (en frío), medida bien

**Cómo se mide.**

- Un proceso nuevo por grafo y versión.
- El codificador de tokens precargado, que en un servidor ya lo estaría.
- Grafo recién cargado y una consulta (puntuar y seleccionar a 4000 en markdown).
- Mediana de 3 ejecuciones.
- La v1.3 sale del tag `v1.3.0` con `git archive`.

| Grafo | v1.3 | v1.4 | Límite del criterio | v1.4 / v1.3 |
|---|---|---|---|---|
| PrismaStats | 27,5 ms | 41,7 ms | 120 ms | 1,52× |
| hono | 42,1 ms | **70,2 ms** | **63 ms** | **1,67×** |
| slurp | 73,3 ms | 96,8 ms | 113 ms | 1,32× |
| tokio | 131,3 ms | **209,4 ms** | **197 ms** | **1,59×** |
| prometheus | 275,5 ms | 319,2 ms | 428 ms | 1,16× |
| sqlalchemy | 651,3 ms | 831,0 ms | 1.004 ms | 1,28× |

**No cumple el límite en frío en hono ni en tokio.**

## En caliente

**Cómo se mide.** Mediana de 20 consultas tras una de calentamiento.

| Grafo | v1.3 | v1.4 | Límite |
|---|---|---|---|
| PrismaStats | 10 ms | 14–15 ms | 15 ms |
| hono | 17 ms | 11–12 ms | 22 ms |
| slurp | 33 ms | 24 ms | 41 ms |
| tokio | 75 ms | 27–28 ms | 94 ms |
| prometheus | 113 ms | 43 ms | 141 ms |
| sqlalchemy | 284 ms | 87–89 ms | 356 ms |

**Cumple, aunque PrismaStats queda justo en el límite.** En los grafos grandes la v1.4 es de 2,5 a
3 veces más rápida: tokeniza una vez por grafo, mientras que la v1.3 retokenizaba todas las
etiquetas en cada consulta.

## De dónde sale el coste extra en frío

**Medido por partes** en un proceso nuevo, en ms:

| Parte | hono v1.3 | hono v1.4 | tokio v1.3 | tokio v1.4 |
|---|---|---|---|---|
| PageRank global | 17,6 | 17,9 | 41,2 | 43,0 |
| Tests e imports fuera | 2,9 | 2,7 | 18,3 | 17,6 |
| Tokens (v1.3: dentro de cada consulta) | — | 9,9 + 1,8 | — | 48,4 + 11,0 |
| Listas del paseo | — | 2,0 | — | 20,3 |
| Puntuar (resto) | 15,2 | 15,6 | 60,5 | 16,0 |
| `build_context` | 6,0 | **19,8** | 9,7 | **57,3** |

**Lo que más pesa es `build_context`.**

- El orden nuevo pone delante nodos bien conectados entre sí, así que la selección incluye muchas
  más relaciones.
- Las relaciones son mejor contexto, pero en un proceso nuevo cada texto se tokeniza para conocer
  su coste.
- Con la caché de costes caliente, ese extra desaparece.
- No se puede reducir desde el scorer: haría falta cambiar cómo `budget.py` calcula esos costes,
  por ejemplo en lotes, sin que cambie la salida.

## Corrección de la puerta

En la puerta (`puerta.md`), la latencia en frío del candidato se midió **en segundo lugar, en el
mismo proceso**, después de la v1.3.

- La caché de costes de `budget.py` no depende del grafo, así que ya estaba caliente.
- Eso favoreció al candidato.
- Medido como arriba, el prototipo tampoco habría cumplido en hono ni en tokio.

**La condición de latencia de la puerta no se cumplía de verdad.** El recall de la validación no se
ve afectado, pero el criterio completo de §6 no se cumple: falla la condición 3, la latencia en
frío.
