import { useCallback, useLayoutEffect, useMemo, useState } from "react";
import { api, Matrix, StaleDerivation } from "./api";

type RequestScope = {
  active: boolean;
  controller: AbortController | null;
  pending: Promise<void> | null;
};
const emptyDerivations: StaleDerivation[] = [];

/** Own table requests by project and kind, sharing concurrent refreshes. */
export function useTableData(
  projectId: string | undefined,
  kind: string,
  enabled: boolean,
  onError: (message: string) => void,
) {
  const scope = useMemo<RequestScope>(
    () => ({ active: false, controller: null, pending: null }),
    [projectId, kind, enabled],
  );
  const [result, setResult] = useState<{
    scope: RequestScope;
    matrix: Matrix;
    stale: StaleDerivation[];
  } | null>(null);

  useLayoutEffect(() => {
    scope.active = true;
    return () => {
      scope.active = false;
      scope.controller?.abort();
      scope.controller = null;
      scope.pending = null;
    };
  }, [scope]);

  const clearMatrix = useCallback(() => {
    scope.controller?.abort();
    scope.controller = null;
    scope.pending = null;
    setResult(null);
  }, [scope]);

  const loadMatrix = useCallback((force = true): Promise<void> => {
    if (!enabled || !projectId || !kind || !scope.active) return Promise.resolve();
    if (scope.pending && !force) return scope.pending;
    scope.controller?.abort();
    const controller = new AbortController();
    scope.controller = controller;
    const current = () => scope.active && scope.controller === controller;
    scope.pending = (async () => {
      try {
        const [matrix, stale] = await Promise.all([
          api<Matrix>(`/api/matrix/${encodeURIComponent(kind)}`, { signal: controller.signal }),
          api<{ stale_derivations: StaleDerivation[] }>(
            `/api/stale-derivations?entity_kind=${encodeURIComponent(kind)}`,
            { signal: controller.signal },
          ),
        ]);
        if (current()) setResult({ scope, matrix, stale: stale.stale_derivations });
      } catch (error) {
        if (current() && !controller.signal.aborted)
          onError(error instanceof Error ? error.message : "Could not load table");
      } finally {
        if (current()) scope.pending = null;
      }
    })();
    return scope.pending;
  }, [enabled, projectId, kind, scope, onError]);

  return {
    matrix: result?.scope === scope ? result.matrix : null,
    staleDerivations: result?.scope === scope ? result.stale : emptyDerivations,
    loadMatrix,
    clearMatrix,
  };
}
