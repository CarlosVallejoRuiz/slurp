# Aristas de JSX (#87): revisión a mano de la implementación (2026-11-28)

**Qué se revisó.**

- El grafo de la v1.5 (`c210b1d`, `index_project` del árbol) en el HEAD del banco de PrismaStats
  (`e975140`) y excalidraw (`dabd83c`).
- **30 aristas `renders`** (15 por proyecto) y **15 nodos nuevos de `memo`/`forwardRef`** (8 y 7),
  sacados con `random.Random(87)`. Script: `handcheck.py`; evidencia en
  `~/slurp-bench/v15/handcheck/sample.txt`.
- Para cada arista, la evidencia es:
  - la declaración del llamador y del destino;
  - el import (también los de varias líneas);
  - cada elemento JSX del fichero que nombra al destino (`<X`, `<Ns.X`), con la cadena de funciones
    que lo envuelven según tree-sitter directamente, sin pasar por el visitante de slurp.
- Una arista es correcta si:
  - el elemento está en el cuerpo del llamador o en una función anónima suya, que no es nodo
    (callback de `.map`, render prop, `useMemo`);
  - el nombre resuelve al destino por la regla de certeza del diseño (§2).

## Resultado: 30 de 30 aristas y 15 de 15 nodos correctos

| | Arista | JSX | Resuelve por |
|---|---|---|---|
| Rp01 | `DatosAdminClient → SelectTrigger` | L652, 969, 1161, 1178 | import con nombre de `@/components/ui/select` (varias líneas) |
| Rp02 | `PlayerCombobox → CommandList` | L95 | import con nombre de `@/components/ui/command` (varias líneas) |
| Rp03 | `PremiumChatWidget → TypingIndicator` | L234, en un callback de `.map` | mismo fichero |
| Rp04 | `AdminConfiguracionPage → LigasManagement` | L26 | import con nombre de `./ligas-management` |
| Rp05 | `AsistenciaClient → SelectTrigger` | L252 | import con nombre |
| Rp06 | `EntrenamientosPage → EntrenamientosClient` | L39 | default; el símbolo se llama como el binding |
| Rp07 | `CommandDialog → Command` | L30 | el `Command` local (L11); el de `cmdk` se importa como `CommandPrimitive` |
| Rp08 | `PremiumChatWidget → Input` | L249 | import con nombre |
| Rp09 | `renderWizardStep → WizardHeader` | 8 usos, todos en `renderWizardStep`, función anidada que es nodo | mismo fichero |
| Rp10 | `SeasonManagement → Input` | L88 | import con nombre |
| Rp11 | `JugadorDetalleClient → OutcomeBadge` | L650, en un callback de `.map` | mismo fichero |
| Rp12 | `OutroScene → UnderlinedText` | L16 | import con nombre (`.agents/…`, un corpus de pruebas dentro del proyecto) |
| Rp13 | `AsistenciaClient → SelectContent` | L255 | import con nombre |
| Rp14 | `JugadoresClient → AddJugadorFields` | L385, 435 | mismo fichero |
| Rp15 | `LigasManagement → Table` | L132 | import con nombre (varias líneas) |
| Re01 | `ZoomMenu → DropdownMenu` | L957 `<DropdownMenu open>`; los `<DropdownMenu.Item>` no dan arista (objeto sin namespace) | default (`export default DropdownMenu`) |
| Re02 | `Picker → CustomColorList` | L187, en la función de `React.forwardRef` (Picker es W2) | import con nombre |
| Re03 | módulo `lasso.test → Excalidraw` | L43, en el callback de `beforeEach`: nivel de módulo | import con nombre; `Excalidraw` es W2 (`React.memo`) |
| Re04 | `MobileShapeActions → CombinedTextProperties` | L842; el uso de L694 es de `CompactShapeActions` (otra arista) | mismo fichero |
| Re05 | `Toolbar → LockButton` | L275 | import con nombre |
| Re06 | `MatchListBase → ListItem` | L503, 523, en callbacks de `.map` | mismo fichero |
| Re07 | módulo `multiPointCreate.test → Excalidraw` | 6 usos en callbacks de `describe`/`it` | import con nombre |
| Re08 | módulo `actionExport → IconButton` | L380, en `PanelComponent: () => …` de un objeto literal, que no es nodo | import con nombre; las llamadas de ese callback (`t`, `useEditorInterface`) ya iban al módulo en la v1.4 |
| Re09 | `ElementLinkDialog → DialogActionButton` | L160, 170 | default (`export default DialogActionButton`) |
| Re10 | `ExcalidrawWrapper → ExcalidrawPlusPromoBanner` | L1019, en una render prop | import con nombre |
| Re11 | `Toolbar → HintViewer` | L255 | import con nombre |
| Re12 | `CommandPaletteInner → LibraryItemIcon` | L233, en el callback de `useMemo` | mismo fichero (declarado después, L973) |
| Re13 | `LayerUI → DefaultMainMenu` | L501 | mismo fichero |
| Re14 | `Stats → StatsInner` | L61 | mismo fichero; `StatsInner = memo(…)` es W2 |
| Re15 | `UserList → ScrollableList` | L248, en el callback de `firstNCollaborators.map` | import con nombre |

**Nodos W2.** Los 15 son `const X = W(fn)` con `W` el `memo` o `forwardRef` de React:

- PrismaStats: `React.forwardRef<…>` de shadcn/ui con `import * as React from "react"`. Son
  `TableHeader`, `PopoverContent`, `ScrollArea`, `SelectSeparator`, `SheetOverlay`,
  `AlertDialogOverlay`, `TableBody` y `SheetDescription`.
- excalidraw:
  - `React.memo` en `icons.tsx` (`StrokeStyleSolidIcon`, `ArrowheadCardinalityExactlyOneIcon`,
    `TextAlignBottomIcon`, `ArrowheadCardinalityManyIcon`);
  - `forwardRef` importado con nombre (`ColStack`);
  - `memo` importado con nombre (`LibraryMenuSection`);
  - `Excalidraw = React.memo(ExcalidrawBase, areEqual)`, que es `memo(Y)` y renderiza a
    `ExcalidrawBase`. El import de React de `index.tsx` ocupa varias líneas.
- **Todos los nodos nuevos, no solo la muestra:** en PrismaStats los 56 llevan `memo`/`forwardRef`
  en la línea de su declaración. En excalidraw lo llevan 47 de 49. Los otros dos (`AppMainMenu`,
  `AppWelcomeScreen`) son `export const X: React.FC<{…}> = React.memo(…)`: la anotación de tipo
  baja el `React.memo` unas líneas. También son correctos.

## La implementación frente al prototipo (`proto_vs_real.py`)

El prototipo W2 (`w2.py` + `jsx.py`, con el indexador de la v1.4.0) se vuelve a ejecutar sobre el
mismo HEAD y se compara con la implementación, nodo a nodo y arista a arista.

| | prototipo | v1.5 | en los dos |
|---|---|---|---|
| PrismaStats: nodos nuevos / aristas `renders` | 56 / 337 | 56 / 337 | 56 / 337 |
| excalidraw: nodos nuevos / aristas `renders` | 48 / 501 | 49 / 511 | 48 / 501 |

La v1.5 contiene todo lo del prototipo. Las 11 diferencias de excalidraw son ganancias; se
comprobaron todas en el fuente y las 11 son ciertas:

- **9 aristas** que el prototipo perdía cuando el fichero importa dos veces del mismo módulo, una
  vez valores y otra tipos. Ejemplo: `import { Dialog } from "./Dialog"` más
  `import type { DialogProps } from "./Dialog"`. Son:
  - `ConfirmDialog → Dialog`;
  - `App.render → ContextMenu`;
  - `TTDChatPanel` y `TTDPreviewPanel → TTDDialogPanel`;
  - `DropdownMenuItemCheckbox → DropdownMenuItem`;
  - `FontSize`, `MultiFontSize`, `Position` y `MultiPosition → StatsDragInput` (default, que se
    llama como el binding).
- **1 nodo y 1 arista en ficheros `.js`** de `dev-docs/`:
  - el nodo `Excalidraw = React.forwardRef(…)` de `ReactLiveScope/index.js`;
  - la arista `HomepageFeatures → Feature` de `Homepage/index.js`.

  Un `.js` con JSX da a la gramática de TypeScript un árbol con nodos `ERROR`, así que se indexa
  por la rama regex, y esa rama da JSX y W2. **El §2 del diseño decía «JSX en ficheros `.js`:
  no».** La realidad: sí, con la cobertura de la rama regex, porque un `.js` con JSX siempre cae a
  ella. No hay falsos positivos: son las mismas reglas de certeza.

**Bandas** (`bands_real.py`, con el `_risk_level` de explain sobre los nodos de `.tsx`/`.jsx`, sin
imports ni tests):

| | v1.4 | v1.5 | prototipo W2 |
|---|---|---|---|
| PrismaStats (LOW / MEDIUM / HIGH) | 530 / 5 / 1 de 536 | 540 / 40 / 12 de 592 | idéntico a la v1.5 |
| PrismaStats: nodos que ya existían y suben | | 10 → MEDIUM, 2 → HIGH | igual |
| PrismaStats: banda de los nodos nuevos | | 22 / 25 / 9 | igual |
| excalidraw (LOW / MEDIUM / HIGH) | 796 / 51 / 7 de 854 | 785 / 97 / 20 de 902 | 787 / 95 / 20 |
| excalidraw: nodos que ya existían y suben | | 42 → MEDIUM, 9 → HIGH | 40, 9 |
| excalidraw: banda de los nodos nuevos | | 40 / 4 / 4 | igual |

Los dos nodos de excalidraw que suben de más salen de las 9 aristas de arriba. Los más
dependidos siguen siendo los mismos: `Button` 32, `Badge` 17, `Input` 13, `IconButton` 30.
`Dialog` pasa de 11 a 12 y `DropdownMenuItem` de 10 a 11.

## explain e impact de los componentes más usados

El antes es el CLI de la v1.4.0 sobre el grafo v1.4 del banco. El después es el CLI del árbol
sobre su índice del mismo HEAD. Los dos son estructurales (`--no-llm`, `--no-refresh`); salidas en
`~/slurp-bench/v15/impact-explain/`.

| | v1.4: explain | v1.4: impact | v1.5: explain | v1.5: impact |
|---|---|---|---|---|
| `Button` | sin nodo: cae en el módulo `button.tsx`, LOW | LOW, 0 ficheros | **HIGH**, 32 llamadores de producción | **HIGH**, 43 ficheros (directos 32, transitivos 19) |
| `Badge` | `Badge()`, nadie la referencia, LOW | LOW, 0 ficheros | **HIGH**, 17 | **HIGH**, 29 ficheros (17, 14) |
| `Input` | sin nodo: cae en el módulo `input.tsx`, LOW | LOW, 0 ficheros | **HIGH**, 13 | **HIGH**, 22 ficheros (13, 10) |
| `IconButton` | sin nodo: cae en el módulo `IconButton.tsx`, LOW | LOW, 0 ficheros | **HIGH**, 30 | **HIGH**, 35 ficheros (30, 13) |

- `explain` e `impact` dan la misma cifra de dependientes directos.
- `IconButton()` aparece con su propia arista saliente (`→ Spinner()`), señal de que el nodo W2 es
  dueño del cuerpo de la función envuelta.
- **Contraste independiente de `Button`:** un grep de los ficheros que importan `Button` de
  shadcn/ui y escriben `<Button` da los mismos 25 ficheros que las 32 aristas `renders` del grafo.
