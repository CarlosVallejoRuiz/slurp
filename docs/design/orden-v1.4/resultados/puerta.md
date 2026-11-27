# v1.4 — puerta en derivación (2026-11-27), registrada antes de abrir la validación

> **Corrección posterior (2026-11-27): la latencia en frío se midió con sesgo.** El candidato se
> midió después de la v1.3, en el mismo proceso, con la caché de costes de `budget.py` ya
> caliente. Medido en un proceso nuevo por grafo, no cumple el límite en frío en hono (70 ms
> frente a 63) ni en tokio (209 ms frente a 197). Así que la condición de latencia de esta puerta
> no se cumplía. Detalles en `latencia.md`. El recall de abajo no cambia.

## Selección hacia delante (§3, Combinaciones)

Las tablas completas están en `derivacion.md`.

| Paso | Configuración | Recall medio a 4000 md, 7 proyectos | Ganancia |
|---|---|---|---|
| Base | v1.3 | 0,532 | — |
| Ronda A, la mejor sola | V3, w_path 1,0 | 0,662 | +0,130 |
| Ronda B, entra V2 | V3 + V2 (reinicio 0,15, r 1,0, k 10) | **0,725** | +0,063 sobre la ronda |
| Ronda C | Nada llega a +0,01 | — | V5 +0,007, V1 +0,005, V6 +0,003, V4 +0,002 |

**Candidato:** `V3#2+V2#2`. Es V3 (rutas y ámbito como términos, w_path 1,0, sobre el TF-IDF de la
v1.3) más V2 (PageRank personalizado dirigido: reinicio 0,15, `calls` hacia atrás con peso 1,0, 10
semillas, empuje local con ε = 1e-4).

- **V1 no entra:** la regla del docstring no se aplica.
- **V6 no entra:** su condición no se aplica.

## La puerta

**Recall por proyecto** (v1.3 → candidato):

| Proyecto | v1.3 | Candidato | Diferencia |
|---|---|---|---|
| ky | 0,901 | 0,959 | +0,058 |
| PrismaStats | 0,413 | 0,527 | +0,114 |
| hono | 0,670 | 0,891 | +0,221 |
| jsoup | 0,525 | 0,646 | +0,121 |
| caddy | 0,381 | 0,714 | +0,333 |
| tokio | 0,461 | 0,800 | +0,339 |
| sqlalchemy | 0,372 | 0,538 | +0,166 |
| slurp, informativo | 0,773 | 0,834 | +0,061 |

**Las tres condiciones:**

- **Ganancia media: +0,193.** Hacía falta +0,05.
- **Peor proyecto: +0,057** (ky). El límite es −0,03.
- **Latencia: dentro de los límites en los seis grafos, en dos medidas seguidas.**

| Grafo | v1.3 en caliente | Candidato en caliente (límite) | v1.3 en frío | Candidato en frío (límite) |
|---|---|---|---|---|
| PrismaStats | 10 ms | 14 ms (15) | 77 ms | 32 ms (120) |
| hono | 17 ms | 11 ms (22) | 40 ms | 52 ms (63) |
| slurp | 33 ms | 21 ms (41) | 71 ms | 82 ms (113) |
| tokio | 75 ms | 23 ms (94) | 114 ms | 152 ms (197) |
| prometheus | 113 ms | 35 ms (141) | 262 ms | 242 ms (428) |
| sqlalchemy | 284 ms | 67 ms (356) | 632 ms | 767 ms (1.004) |

**PUERTA: PASA**, tal como se midió entonces. Ver la corrección arriba: con la medida justa, la latencia en frío no pasa.

## Cómo se llegó a cumplir la latencia

- **La primera medida del prototipo no cumplía en frío:** hono 72 ms, tokio 251 ms y sqlalchemy
  1.185 ms. Tokenizaba la ruta de cada nodo por separado y creaba contadores para todos los nodos.
- **Se optimizó sin cambiar ningún score:**
  - tokenización por fichero y por ámbito;
  - contadores y listas de postings solo cuando se piden;
  - el bucle del empuje local con variables locales, en el mismo orden y con las mismas
    operaciones.
- **Comprobado:** 162 pares (pregunta, configuración) de cinco proyectos dan scores idénticos antes
  y después.
- **El margen de PrismaStats en caliente es estrecho** (14 frente a 15 ms) y lo marca el PPR. La
  implementación en slurp tiene que volver a medirse.
- **Por qué el candidato es más rápido en caliente:** cachea la tokenización por grafo, mientras
  que la v1.3 vuelve a tokenizar todas las etiquetas en cada consulta.
