# Rama regex de TypeScript: aristas `calls` frente a tree-sitter (2026-11-28)

**Qué se midió.**

- Cada arista `calls` que sale de un fichero TS/JS en la rama regex se compara con las de
  tree-sitter, en el HEAD del banco de PrismaStats (`e975140`) y excalidraw (`dabd83c`), con el
  mismo código para las dos ramas.
- Los nodos regex se traducen a los de tree-sitter por (fichero, nombre, línea), porque la regex
  nombra distinto las funciones anidadas y los métodos de campo flecha.
- Script: `regex_classify.py`. Tres estados:
  - antes: la v1.4;
  - con los dos arreglos de `55dad2f`: blanqueo que conserva las líneas y parámetros antes del
    cuerpo;
  - con el tercero: silencio ante un nombre declarado dos veces en el mismo fichero.

| | antes | 2 arreglos | 3 arreglos |
|---|---|---|---|
| **PrismaStats: mal atribuidas** | **20** | **2** | **0** |
| PrismaStats: iguales a tree-sitter | 154 | 337 | 337 |
| PrismaStats: iguales con otro nombre de nodo | 32 | 37 | 36 |
| PrismaStats: llamadas a la propia función anidada | 12 | 75 | 73 |
| PrismaStats: extremo que tree-sitter no emite | 0 | 2 | 2 |
| PrismaStats: aristas regex / tree-sitter | 218 / 447 | 453 / 447 | 448 / 447 |
| **excalidraw: mal atribuidas** | **100** | **0** | **0** |
| excalidraw: iguales a tree-sitter | 235 | 661 | 649 |
| excalidraw: extremo que tree-sitter no emite | 166 | 428 | 427 |
| excalidraw: aristas regex / tree-sitter | 507 / 3.824 | 1.110 / 3.825 | 1.098 / 3.825 |

## Qué queda distinto, y por qué no es falso

- **Llamadas a la propia función anidada** (`ClubesAdminClient → handleToggleStatus`): son
  reales. En tree-sitter la función anidada cuelga de su padre y gana el `contains` (una arista
  por par); la regex la emite a nivel de fichero.
- **Extremo que tree-sitter no emite:** sobre todo métodos escritos como campo flecha
  (`setUserToFollow = () => …` en `Collab`), que la regex emite y tree-sitter no, y funciones
  declaradas dentro de funciones flecha. En PrismaStats, las 2 comprobadas a mano son ciertas:
  - `load` define y llama a `sortByPos`;
  - `POST` define `send` dentro de un callback y la llama.
- **Huecos:** la regex sigue viendo bastante menos que tree-sitter (448 frente a 447 aristas en
  PrismaStats, pero con otros nodos; 1.098 frente a 3.825 en excalidraw).
- **Las 2 mal atribuidas con los dos arreglos** salían de un tercer defecto, anterior:
  - la regex aplana a nivel de fichero las funciones flecha anidadas, así que `getGolesFavor`,
    declarada en `getDashboardData` y en `getRendimientoData`, era un solo nodo (el primero);
  - las llamadas de `getRendimientoData` a su propia copia caían en la del otro.

  Arreglado con la regla de los resolutores ante la ambigüedad, el silencio: un nombre declarado
  dos veces en un fichero no es destino de ninguna llamada. Cuesta 12 aristas correctas en
  excalidraw.
- **La diferencia de una arista de tree-sitter en excalidraw** entre «antes» y después: 8 ficheros
  de excalidraw dan un árbol con nodos `ERROR` y se indexan por la rama regex aunque haya
  tree-sitter. Los arreglos les afectan.
