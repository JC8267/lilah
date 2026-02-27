import { useEffect, useMemo } from 'react';
import { SlidersHorizontal, X } from 'lucide-react';
import { useFilterStore } from '../../stores/filter-store';

export function FilterSidebar() {
  const { dimensions, activeFilters, setDimensions, addFilter, removeFilter, clearFilters } =
    useFilterStore();

  useEffect(() => {
    fetch('/api/metadata/demographics')
      .then((r) => r.json())
      .then(setDimensions)
      .catch(() => {});
  }, [setDimensions]);

  // Group dimensions by demo_id category (strip prefix like TOTAL:, CUSTOMER:, etc.)
  const groups = useMemo(() => {
    const map = new Map<string, { demo_id: string; demo_level: string }[]>();
    for (const d of dimensions) {
      // Use demo_id as group key
      if (!map.has(d.demo_id)) map.set(d.demo_id, []);
      map.get(d.demo_id)!.push(d);
    }
    // Filter to TOTAL: dimensions only for cleaner UX
    const totalGroups = new Map<string, { demo_id: string; demo_level: string }[]>();
    for (const [key, vals] of map) {
      if (key.startsWith('TOTAL:') || key === 'Total') {
        totalGroups.set(key, vals);
      }
    }
    return totalGroups;
  }, [dimensions]);

  return (
    <div className="border-t border-[var(--color-border)] p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          <SlidersHorizontal className="w-3.5 h-3.5 text-[var(--color-text-secondary)]" />
          <span className="text-xs font-medium text-[var(--color-text-secondary)]">
            Filters
          </span>
        </div>
        {activeFilters.length > 0 && (
          <button
            onClick={clearFilters}
            className="text-xs text-red-500 hover:underline"
          >
            Clear
          </button>
        )}
      </div>

      {/* Active filter chips */}
      {activeFilters.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-2">
          {activeFilters.map((f) => (
            <span
              key={f.demo_id}
              className="inline-flex items-center gap-1 text-xs bg-[var(--color-primary-light)] text-[var(--color-primary)] px-2 py-0.5 rounded-full"
            >
              {f.demo_level.replace(/^(TOTAL|CUSTOMER|PROSPECT): /, '')}
              <button onClick={() => removeFilter(f.demo_id)}>
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Filter dropdowns */}
      <div className="space-y-1.5 max-h-48 overflow-y-auto">
        {Array.from(groups.entries())
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([groupId, values]) => {
            const label = groupId.replace(/^TOTAL: /, '');
            const active = activeFilters.find((f) => f.demo_id === groupId);
            return (
              <select
                key={groupId}
                value={active?.demo_level || ''}
                onChange={(e) => {
                  if (e.target.value) {
                    addFilter({ demo_id: groupId, demo_level: e.target.value });
                  } else {
                    removeFilter(groupId);
                  }
                }}
                className="w-full text-xs border border-[var(--color-border)] rounded px-2 py-1 bg-[var(--color-surface)]"
              >
                <option value="">{label}</option>
                {values.map((v) => (
                  <option key={v.demo_level} value={v.demo_level}>
                    {v.demo_level.replace(/^(TOTAL|CUSTOMER|PROSPECT): /, '')}
                  </option>
                ))}
              </select>
            );
          })}
      </div>
    </div>
  );
}
