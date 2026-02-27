import { create } from 'zustand';
import type { DemoFilter, DemoDimension } from '../types';

interface FilterState {
  dimensions: DemoDimension[];
  activeFilters: DemoFilter[];
  setDimensions: (dims: DemoDimension[]) => void;
  addFilter: (filter: DemoFilter) => void;
  removeFilter: (demo_id: string) => void;
  clearFilters: () => void;
}

export const useFilterStore = create<FilterState>((set) => ({
  dimensions: [],
  activeFilters: [],

  setDimensions: (dims) => set({ dimensions: dims }),

  addFilter: (filter) =>
    set((s) => ({
      activeFilters: [
        ...s.activeFilters.filter((f) => f.demo_id !== filter.demo_id),
        filter,
      ],
    })),

  removeFilter: (demo_id) =>
    set((s) => ({
      activeFilters: s.activeFilters.filter((f) => f.demo_id !== demo_id),
    })),

  clearFilters: () => set({ activeFilters: [] }),
}));
