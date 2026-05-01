# -*- coding: utf-8 -*-
# File: modular_shear_tab_minor_datagen_multiprocessing.py
# Flowchart-compliant data generation for single-plate (shear tab) MINOR-axis connection
# WITH MULTIPROCESSING SUPPORT

import csv
import sys
import os
import time
from collections import defaultdict
from typing import List, Tuple, Dict
from math import pi
import random
import pandas as pd
from multiprocessing import Pool, Manager, cpu_count
from functools import partial

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Modular solver imports (MINOR axis)
from connections.shear_connection.shear_tab_minor.solver import ShearTabMinorSolver
from connections.shear_connection.shear_tab_minor.models import (
    ShearTabMinorInput, BoltData, PlateData, BeamWebDataMinor,
    Support, WeldData, Demand
)

class ShearTabMinorDataGenerator:
    """
    Flow per chart:
      Start → Define BEAM (random from DB) → Define COLUMN (random from DB)
      → check ratios (bb ≤ bc, bb ≥ 0.5bc, hb ≤ 2hc, hb ≥ 0.5hc)
      → define Vu (incremental)
      → define multiple connections (incremental)
      → Geometry Check → Strength Check (UCmax)
      → if UCmax ≤ 1.0:
           → CASCADE ALGORITHM: Find optimal target configuration
           → Priority 1: Perfect match (UC 0.5-0.95) - stop and save lightest
           → Priority 2: Minimum legal config (even if UC < 0.5) - save for light loads
           → Priority 3: Lightest feasible (fallback)
           → Save ONLY 1 target config per (Vu, beam, column)
      → write to DB
      
    MULTIPROCESSING VERSION: Parallelizes work across CPU cores
    """

    def __init__(self, random_seed: int = 2025, pair_limit: int = 300, n_processes: int = None):
        self.random_seed = random_seed
        self.rng = random.Random(random_seed)
        self.pair_limit = pair_limit
        self.n_processes = n_processes if n_processes else max(1, cpu_count() - 1)

        # Progress
        self.start_time = None
        self.current_vu = None
        self.vu_start_time = None
        self.processed_combinations = 0
        self.progress_interval = 2000  # print every N combinations

        # Constants
        self.STEEL_DENS = 0.283        # lb/in^3
        self.ASSUMED_BOLT_LENGTH = 3.0 # in

        # Materials [ksi]
        self.mat_plate  = {'A992': {'Fy': 50.0, 'Fu': 65.0}}
        self.mat_beam   = {'A992': {'Fy': 50.0, 'Fu': 65.0}}
        self.mat_bolt   = {
        #A325X or A490N 68 #A325N 54 #A490X 84 #A307 27
            # 'A307': {'Fy': 92.0, 'Fu': 120.0, 'F_nv': 27.0, 'catalog_grade': 'A307' }
           'A325N': {'Fy': 92.0, 'Fu': 120.0, 'F_nv': 54.0, 'catalog_grade': 'A325N' }
        }
        self.mat_column = {'A992': {'Fy': 50.0, 'Fu': 65.0}}

        # Enumerations (incremental)
        self.bolt_ds      = [0.5, 0.625, 0.75, 0.875, 1.00, 1.125, 1.25, 1.375, 1.5]
        self.n_verts      = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        self.n_rows_list  = [1, 2]
        
        self.plate_ts = [0.25, 0.3125,0.375, 0.4375, 0.5, 0.5625, 0.875, 1.5]
        
        # Proper weld sizes per AISC
        self.weld_legs = [ 0.1875, 0.25, 0.3125, 0.375]
        
        # TWO-RANGE ADAPTIVE SAMPLING
        # Range 1: Low Vu (10-50): 5 kip increments
        # Range 2: Critical zone (50-300): 1 kip increments for very dense transition coverage
        
        # Low Vu: 5 kip increments
        vu_low = list(range(10, 50, 2))  # [10, 15, 20, 25, 30, 35, 40, 45]
        
        # Critical zone: 1 kip increments (very dense sampling for all transitions)
        vu_critical = list(range(50, 301, 1))  # [50, 51, 52, 53, ..., 299, 300]
        
        # Combine (50 appears in both, but set() removes duplicates)
        self.Vu_values = sorted(list(set(vu_low + vu_critical)))
        
        # Result:
        # - 10-50: 8 values (5 kip spacing)
        # - 50-300: 251 values (1 kip spacing) ← VERY DENSE COVERAGE
        # Total: ~259 values
        
        print(f"Generated {len(self.Vu_values)} Vu values")
        print(f"  Low (10-50): {len([v for v in self.Vu_values if 10 <= v < 50])} values (5 kip spacing)")
        print(f"  Critical (50-300): {len([v for v in self.Vu_values if 50 <= v <= 300])} values (1 kip spacing)")

        
        # # Geometry fixed values (absolute, not multiplied by d)
        # self.pitch_values = [3.0, 4.0, 5.0, 6.0]
        # self.gage_values  = [3.0, 4.0, 5.0, 6.0]
        # self.Lev_values   = [1.25, 1.5, 1.75, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        # self.Leh_values  =  [1.5, 1.75, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        # self.a_values     = [2.0]

         # Geometry fixed values (absolute, not multiplied by d)
        self.pitch_values = [3.0, 4.0, 5.0, 6.0]
        self.gage_values  = [3.0, 4.0, 5.0, 6.0]
        self.Lev_values   = [1.25, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        self.Leh_values  =  [1.5, 2.0, 3.0]
        self.a_values     = [2.0]

        # UC band for optimization
        self.MIN_UC = 0.5
        self.MAX_UC = 0.95

        # Load full W-sections DB (no global dimension filtering)
        self._load_sections()

        # Build random pair sample (up to pair_limit valid pairs)
        self._build_random_pair_sample(self.pair_limit)

        self._calc_upper_bound()

    # ---------------------------
    # Sections
    # ---------------------------
    def _load_sections(self):
        """Load W-shapes; do NOT prefilter by size. Only Type=='W' and not-NaN.

        Preference order (all looked for in the script folder):
          1) 'w_section_properties_heavy.csv'  -> your heavy-only W database
          2) 'w_section_properties.csv'        -> full W database
          3) 'W AISC database.*'              -> legacy full database
        """
        try:
            # Prioritize new_wsection_properties.csv (with kdet), then fallback to others
            if os.path.exists('new_wsection_properties.csv'):
                df = pd.read_csv('new_wsection_properties.csv')
                print("Loaded sections from new_wsection_properties.csv (with kdet)")

            # if os.path.exists('new_wsection_propertiessmall2.csv'):
            #     df = pd.read_csv('new_wsection_propertiessmall2.csv')
            #     print("Loaded sections from new_wsection_propertiessmall2.csv (with kdet)")
           
            # elif os.path.exists('w_section_properties.csv'):
            #     df = pd.read_csv('w_section_properties.csv')
            #     print("Loaded sections from w_section_properties.csv")
            # elif os.path.exists('w_section_properties.csv'):
            #     df = pd.read_csv('w_section_properties.csv')
            #     print("Loaded sections from w_section_properties.csv") 
            # elif os.path.exists('W AISC database.csv'):
            #     df = pd.read_csv('W AISC database.csv')
            #     print("Loaded sections from W AISC database.csv")
            # elif os.path.exists('/mnt/data/W AISC database.csv'):
            #     df = pd.read_csv('/mnt/data/W AISC database.csv')
            #     print("Loaded sections from /mnt/data/W AISC database.csv")
            # elif os.path.exists('W AISC database.xlsx'):
            #     df = pd.read_excel('W AISC database.xlsx')
            #     print("Loaded sections from W AISC database.xlsx")
            else:
                raise FileNotFoundError("No section database found.")

            # normalize names
            cols = {c.lower(): c for c in df.columns}
            def pick(*names):
                for n in names:
                    if n.lower() in cols:
                        return cols[n.lower()]
                raise KeyError(f"Missing one of: {names}")

            c_label = pick('AISC_Manual_Label', 'label', 'name')
            c_d     = pick('d', 'depth')
            c_bf    = pick('bf', 'flange_width')
            c_tw    = pick('tw', 'web_thk', 'web')
            c_tf    = pick('tf', 'flange_thk', 'flange')
            c_A     = pick('A', 'area')
            c_kdet  = pick('kdet', 'k_det', 'k')  # K-distance from AISC (for clear depth calculation)
            
            # Try to get weight column (W in lb/ft) if available
            try:
                c_W = pick('W', 'weight', 'Weight')
                has_weight = True
            except KeyError:
                has_weight = False

            # Check if Type column exists (for xlsx), otherwise assume all are W-sections (for csv)
            if 'type' in cols:
                c_type = pick('Type')
                if has_weight:
                    w = df[df[c_type].astype(str).str.upper().str.strip() == 'W'][
                        [c_label, c_d, c_bf, c_tw, c_tf, c_A, c_W, c_kdet]
                    ].dropna().rename(columns={
                        c_label: 'AISC_Manual_Label',
                        c_d: 'd', c_bf: 'bf', c_tw: 'tw', c_tf: 'tf', c_A: 'A', c_W: 'W', c_kdet: 'kdet'
                    }).reset_index(drop=True)
                else:
                    w = df[df[c_type].astype(str).str.upper().str.strip() == 'W'][
                        [c_label, c_d, c_bf, c_tw, c_tf, c_A, c_kdet]
                    ].dropna().rename(columns={
                        c_label: 'AISC_Manual_Label',
                        c_d: 'd', c_bf: 'bf', c_tw: 'tw', c_tf: 'tf', c_A: 'A', c_kdet: 'kdet'
                    }).reset_index(drop=True)
            else:
                # w_section_properties.csv is already filtered to W-sections only
                if has_weight:
                    w = df[[c_label, c_d, c_bf, c_tw, c_tf, c_A, c_W, c_kdet]].dropna().rename(columns={
                        c_label: 'AISC_Manual_Label',
                        c_d: 'd', c_bf: 'bf', c_tw: 'tw', c_tf: 'tf', c_A: 'A', c_W: 'W', c_kdet: 'kdet'
                    }).reset_index(drop=True)
                else:
                    w = df[[c_label, c_d, c_bf, c_tw, c_tf, c_A, c_kdet]].dropna().rename(columns={
                        c_label: 'AISC_Manual_Label',
                        c_d: 'd', c_bf: 'bf', c_tw: 'tw', c_tf: 'tf', c_A: 'A', c_kdet: 'kdet'
                    }).reset_index(drop=True)

            # Use ALL W-shapes available (no global size filters)
            self.beam_sections = w.copy()
            self.column_sections = w.copy()

            print(f"Total W-shapes available: {len(w)} (beams) x {len(w)} (columns)")

        except Exception as e:
            print(f"WARNING: {e}; using small defaults.")
            self.beam_sections = pd.DataFrame({
                'AISC_Manual_Label': ['W12X26','W16X31','W18X35','W21X44','W24X55'],
                'd':[12.2,15.9,17.7,20.8,23.6],
                'bf':[6.49,5.53,6.0,6.5,7.01],
                'tw':[0.230,0.275,0.300,0.350,0.395],
                'tf':[0.380,0.440,0.425,0.450,0.505],
                'A':[7.65,9.13,10.3,13.0,16.2]
            })
            self.column_sections = self.beam_sections.copy()

    def _split_by_depth(self, df):
        """Split dataframe into small, medium, large by depth."""
        if len(df) == 0:
            return df.copy(), df.copy(), df.copy()
        
        depth_33 = df['d'].quantile(0.33)
        depth_67 = df['d'].quantile(0.67)
        
        small = df[df['d'] < depth_33].copy()
        medium = df[(df['d'] >= depth_33) & (df['d'] < depth_67)].copy()
        large = df[df['d'] >= depth_67].copy()
        
        return small, medium, large

    def _split_by_weight(self, df):
        """Split dataframe into light, medium, heavy by weight."""
        if len(df) == 0:
            return df.copy(), df.copy(), df.copy()
        
        # Use W (weight) if available, otherwise use A (area) as proxy
        weight_col = 'W' if 'W' in df.columns else 'A'
        
        # Use quantiles to split into thirds
        weight_33 = df[weight_col].quantile(0.33)
        weight_67 = df[weight_col].quantile(0.67)
        
        light = df[df[weight_col] < weight_33].copy()
        medium = df[(df[weight_col] >= weight_33) & (df[weight_col] < weight_67)].copy()
        heavy = df[df[weight_col] >= weight_67].copy()
        
        return light, medium, heavy

    def _build_random_pair_sample(self, k_pairs: int):
        """Build realistic beam-column pairs using depth and weight categories."""
        
        # Step 1: Split by depth
        beam_depth_small, beam_depth_medium, beam_depth_large = self._split_by_depth(self.beam_sections)
        col_depth_small, col_depth_medium, col_depth_large = self._split_by_depth(self.column_sections)
        
        # Step 2: Split each depth category by weight (creates 3 × 3 = 9 categories)
        beam_categories = {}
        for name, df in [('small', beam_depth_small), 
                          ('medium', beam_depth_medium), 
                          ('large', beam_depth_large)]:
            if len(df) > 0:
                light, medium, heavy = self._split_by_weight(df)
                beam_categories[f'{name}_light'] = light
                beam_categories[f'{name}_medium'] = medium
                beam_categories[f'{name}_heavy'] = heavy
        
        col_categories = {}
        for name, df in [('small', col_depth_small), 
                         ('medium', col_depth_medium), 
                         ('large', col_depth_large)]:
            if len(df) > 0:
                light, medium, heavy = self._split_by_weight(df)
                col_categories[f'{name}_light'] = light
                col_categories[f'{name}_medium'] = medium
                col_categories[f'{name}_heavy'] = heavy
        
        # Step 3: STRICT compatible pairing rules
        # Rule: Small/Medium beams can pair with Small/Medium columns only
        #       Large beams can ONLY pair with Large columns
        #       Within each depth category, weight rules apply (beam weight ≤ column weight)
        compatible_pairs = {
            # Small beams - can pair with Small or Medium columns (any weight)
            'small_light': ['small_light', 'small_medium', 'small_heavy',
                           'medium_light', 'medium_medium', 'medium_heavy'],
            'small_medium': ['small_medium', 'small_heavy',
                            'medium_medium', 'medium_heavy'],
            'small_heavy': ['small_heavy',
                           'medium_heavy'],
            
            # Medium beams - can pair with Small or Medium columns (any weight)
            'medium_light': ['small_light', 'small_medium', 'small_heavy',
                            'medium_light', 'medium_medium', 'medium_heavy'],
            'medium_medium': ['small_medium', 'small_heavy',
                             'medium_medium', 'medium_heavy'],
            'medium_heavy': ['small_heavy',
                            'medium_heavy'],
            
            # Large beams - can ONLY pair with Large columns (any weight)
            'large_light': ['large_light', 'large_medium', 'large_heavy'],
            'large_medium': ['large_medium', 'large_heavy'],
            'large_heavy': ['large_heavy']
        }
        
        # Step 4: Build pairs from compatible categories
        all_combinations = []
        
        for beam_cat, beam_df in beam_categories.items():
            if len(beam_df) == 0:
                continue
            compatible_cols = compatible_pairs.get(beam_cat, [])
            for col_cat in compatible_cols:
                if col_cat in col_categories and len(col_categories[col_cat]) > 0:
                    for bi in beam_df.index:
                        for ci in col_categories[col_cat].index:
                            beam = beam_df.loc[bi]
                            col = col_categories[col_cat].loc[ci]
                            # Still apply geometric ratio checks as final filter
                            if self._pair_ratio_ok(beam, col):
                                all_combinations.append((int(bi), int(ci)))
        
        # Step 5: Randomly sample from compatible pairs
        self.rng.shuffle(all_combinations)
        self.valid_pairs = all_combinations[:k_pairs]
        
        print(f"Found {len(self.valid_pairs)} valid beam-column pairs (limit: {k_pairs})")
        print(f"  Strategy: Depth × Weight categorization with STRICT pairing rules")
        print(f"  Rule: Small/Medium beams ↔ Small/Medium columns | Large beams ↔ Large columns only")
        print(f"  Weight rule: Beam weight category ≤ Column weight category")
        print(f"  Constraints: bb≤bc, bb≥0.5bc, hb≤2hc, hb≥0.5hc (research paper limits)")

    def _calc_upper_bound(self):
        # Calculate combinations considering conditional gage usage
        # For n_rows=1: 1 gage value (0.0), for n_rows=2: len(gage_values)
        gage_combinations = 1 + len(self.gage_values)  # 1 for n_rows=1, 4 for n_rows=2
        
        per_config = (len(self.bolt_ds) * len(self.n_verts) * len(self.pitch_values) *
                      gage_combinations * len(self.Lev_values) * len(self.Leh_values) *
                      len(self.a_values) * len(self.mat_bolt) *
                      len(self.plate_ts) * len(self.weld_legs))
        self.total_combinations = per_config * len(self.Vu_values) * len(self.valid_pairs)
        print(f"DEBUG upper-bound combinations: {self.total_combinations:,}")

    # ---------------------------
    # Flowchart helpers
    # ---------------------------
    def _format_time(self, s: float) -> str:
        if s < 60:   return f"{s:.1f}s"
        if s < 3600: return f"{s/60:.1f}m"
        return f"{s/3600:.1f}h"

    def candidate_material_weight(self, c: dict) -> float:
        W_plate = c['plate_h'] * c['plate_l'] * c['t'] * self.STEEL_DENS
        a_leg = c['weld_leg']
        W_weld = c['plate_h'] * (0.5 * a_leg ** 2) * self.STEEL_DENS
        n_bolts = c['rows'] * c['n_vert']
        bolt_vol_each = (pi * c['d']**2 / 4.0) * self.ASSUMED_BOLT_LENGTH
        W_bolts = n_bolts * bolt_vol_each * self.STEEL_DENS
        return W_plate + W_weld + W_bolts


    # Flowchart ratio checks applied *after* random beam+column selection
    # Based on research paper constraints to avoid unrealistic beam-column pairs
    def _pair_ratio_ok(self, beam_row, col_row) -> bool:
        hb, bb = float(beam_row['d']), float(beam_row['bf'])  # Beam: height (depth) and flange width
        hc, bc = float(col_row['d']),  float(col_row['bf'])   # Column: height (depth) and flange width
        
        # Research paper constraints (realistic beam-column pair limits):
        # - bb ≤ bc: Beam flange must fit within column flange
        if not (bb <= bc):
            return False
        
        # - bb ≥ 0.5×bc: Beam flange should be at least 50% of column flange
        if not (bb >= 0.5 * bc):
            return False
        
        # - hb ≤ 2×hc: Beam depth should not exceed 2× column depth
        if not (hb <= 2.0 * hc):
            return False
        
        # - hb ≥ 0.5×hc: Beam depth should be at least 50% of column depth
        if not (hb >= 0.5 * hc):
            return False
        
        return True

    # ---------------------------
    # Worker function for multiprocessing
    # ---------------------------
    @staticmethod
    def _process_single_pair_vu(args: Tuple) -> Dict:
        """
        Worker function to process a single (beam-column pair, Vu) combination.
        This function is executed in a separate process.
        
        Returns a dict with results for this combination.
        """
        # Unpack arguments
        (bi, ci, Vu, beam_data, col_data, 
         mat_plate, mat_beam, mat_bolt, mat_column,
         bolt_ds, n_verts, n_rows_list, plate_ts, weld_legs,
         pitch_values, gage_values, Lev_values, Leh_values, a_values,
         MIN_UC, MAX_UC, STEEL_DENS, ASSUMED_BOLT_LENGTH) = args
        
        # Create a solver instance for this worker (MINOR)
        solver = ShearTabMinorSolver(include_costs=False)
        
        # Extract beam and column properties
        depth  = float(beam_data['d'])
        flange = float(beam_data['tf'])
        web_t  = float(beam_data['tw'])
        # Use AISC kdet (same approach as major-axis data generator) to compute clear depth
        kdet_beam = float(beam_data['kdet'])
        b_lbl  = beam_data['AISC_Manual_Label']
        
        # Minor-axis support parameters: use column flange width and web thickness
        col_flange_width = float(col_data['bf'])
        col_web_thk      = float(col_data['tw'])
        c_lbl          = col_data['AISC_Manual_Label']
        
        group_candidates: List[dict] = []
        vu_attempt = 0
        vu_success = 0
        
        
        def candidate_material_weight(c: dict) -> float:
            W_plate = c['plate_h'] * c['plate_l'] * c['t'] * STEEL_DENS
            a_leg = c['weld_leg']
            W_weld = c['plate_h'] * (0.5 * a_leg ** 2) * STEEL_DENS
            n_bolts = c['rows'] * c['n_vert']
            bolt_vol_each = (pi * c['d']**2 / 4.0) * ASSUMED_BOLT_LENGTH
            W_bolts = n_bolts * bolt_vol_each * STEEL_DENS
            return W_plate + W_weld + W_bolts
        
        # ----- Incremental connection configs -----
        for d in bolt_ds:
            for n_vert in n_verts:
                for a in a_values:
                    for n_rows in n_rows_list:
                        # Determine gage values based on n_rows
                        if n_rows == 1:
                            gage_list = [0.0]  # No gage needed for single row
                        else:  # n_rows == 2
                            gage_list = gage_values
                        
                        for gage in gage_list:
                            for grade in mat_bolt.keys():
                                props = mat_bolt[grade]
                                F_nv = props['F_nv']
                                solver_grade = props.get('catalog_grade', grade)
                                
                                # Move plate thickness loop BEFORE pitch and edge distance loops for efficiency
                                for t in plate_ts:
                                    # AISC minimum weld size for rotational ductility (early filtering)
                                    # Minimum weld size = 5/8 × plate_thickness (Part 10, Fy=50 ksi, FEXX=70 ksi)
                                    # Ensures welds can fully develop the plate (plate yields before weld fails)
                                    min_weld_required = 0.625 * t  # 5/8 = 0.625
                                    valid_weld_legs = [w for w in weld_legs if w >= min_weld_required]
                                    
                                    # AISC maximum spacing check for weathering steel (early filtering)
                                    # Max spacing = min(14*t_thinner, 7.0 inches)
                                    t_thinner = min(t, web_t)
                                    max_spacing = min(14.0 * t_thinner, 7.0)
                                    
                                    # Pre-filter pitch values
                                    valid_pitch = [p for p in pitch_values if p <= max_spacing]
                                    
                                    # AISC maximum edge distance check (early filtering)
                                    # Max edge distance = min(12*t, 6.0 inches)
                                    max_edge_plate = min(12.0 * t, 6.0)
                                    max_edge_web = min(12.0 * web_t, 6.0)
                                    
                                    # Pre-filter Lev values (controlled by plate thickness)
                                    valid_Lev = [Lev for Lev in Lev_values if Lev <= max_edge_plate]
                                    
                                    # Pre-filter Leh values (controlled by more restrictive thickness)
                                    max_edge_horizontal = min(max_edge_plate, max_edge_web)
                                    valid_Leh = [Leh for Leh in Leh_values if Leh <= max_edge_horizontal]
                                    
                                    # Skip this plate thickness if no valid values exist
                                    if not valid_weld_legs or not valid_pitch or not valid_Lev or not valid_Leh:
                                        continue
                                    
                                    # Loop only over valid pitch values
                                    for pitch in valid_pitch:
                                        # Create BoltData here (moved inside pitch loop)
                                        bolt_data_obj = BoltData(
                                            d=d, F_nv=F_nv, n_vert=n_vert,
                                            pitch=pitch, n_rows=n_rows, gage=gage,
                                            grade=solver_grade
                                        )
                                        
                                        # Loop only over valid edge distance values
                                        for Lev in valid_Lev:
                                            for Leh in valid_Leh:
                                                plate_data_obj = PlateData(
                                                    t=t,
                                                    Fy=mat_plate['A992']['Fy'],
                                                    Fu=mat_plate['A992']['Fu'],
                                                    a=a,
                                                    edge_dist_vert=Lev,
                                                    edge_dist_horiz=Leh
                                                )

                                                # For MINOR axis, use the same clear depth philosophy as MAJOR:
                                                # compute clear_depth from actual AISC kdet values in the data generator
                                                # and pass via BeamWebDataMinor.
                                                clear_depth_actual = depth - 2.0 * kdet_beam
                                                beam_data_obj = BeamWebDataMinor(
                                                    t_w=web_t,
                                                    Fu=mat_beam['A992']['Fu'],
                                                    depth_total=depth,
                                                    beam_flange_thk=flange,
                                                    clear_depth=clear_depth_actual,
                                                    Fy=mat_beam['A992']['Fy']
                                                )

                                                support_data = Support(
                                                    col_flange_width=col_flange_width,
                                                    col_web_thk=col_web_thk,
                                                    col_Fy=mat_column['A992']['Fy'],
                                                    col_Fu=mat_column['A992']['Fu']
                                                )

                                                # Early filtering: Calculate plate height and check if it fits
                                                plate_h_early = (n_vert - 1) * pitch + 2.0 * Lev
                                                if plate_h_early > clear_depth_actual:
                                                    continue  # Skip this configuration - plate doesn't fit

                                                for weld_leg in valid_weld_legs:
                                                        
                                                    weld_Fe = 110.0  # Weld electrode strength (ksi)
                                                    weld_data = WeldData(a_leg=weld_leg, Fe=weld_Fe)
                                                    demand_data = Demand(Vu=Vu)

                                                    input_data = ShearTabMinorInput(
                                                        bolt=bolt_data_obj,
                                                        plate=plate_data_obj,
                                                        beam=beam_data_obj,
                                                        support=support_data,
                                                        weld=weld_data,
                                                        demand=demand_data
                                                    )

                                                    # attempt
                                                    vu_attempt += 1

                                                    try:
                                                        result = solver.solve(input_data.to_dict())
                                                        if result.get('status') == 'SUCCESS' and 'result' in result:
                                                            uc_max  = result['result']['UC_max']
                                                            uc_name = result['result'].get('UC_name', '')
                                                            if uc_max is not None and uc_max <= 1.0:
                                                                vu_success += 1
                                                                
                                                                # Extract individual strength check values
                                                                strength_checks = result['result'].get('strength_checks', [])
                                                                uc_values = {}
                                                                for check in strength_checks:
                                                                    check_name = check.get('name', '')
                                                                    ratio = check.get('ratio')
                                                                    
                                                                    # Map check names to field names
                                                                    if 'Bolt group (plate side' in check_name:
                                                                        uc_values['UC_bolt_plate_side'] = ratio
                                                                    elif 'Bolt group (web side' in check_name:
                                                                        uc_values['UC_bolt_web_side'] = ratio
                                                                    elif 'Plate net shear rupture' in check_name:
                                                                        uc_values['UC_plate_net_shear_rupture'] = ratio
                                                                    elif 'Plate block shear' in check_name:
                                                                        uc_values['UC_plate_block_shear'] = ratio
                                                                    elif 'Weld strength' in check_name:
                                                                        uc_values['UC_weld_strength'] = ratio
                                                                    elif 'Plate gross shear yield' in check_name:
                                                                        uc_values['UC_plate_gross_shear_yield'] = ratio
                                                                    elif 'Max plate thickness' in check_name:
                                                                        uc_values['UC_max_plate_thickness'] = ratio
                                                                    elif 'Support at weld' in check_name:
                                                                        uc_values['UC_support_at_weld'] = ratio
                                                                    elif 'Flexural yield & local buckling' in check_name:
                                                                        uc_values['UC_flexural_yield_local_buckling'] = ratio
                                                                    elif 'Flexural rupture of plate' in check_name:
                                                                        uc_values['UC_flexural_rupture'] = ratio
                                                                    elif 'Shear–flexural interaction' in check_name:
                                                                        uc_values['UC_shear_flexural_interaction'] = ratio
                                                                
                                                                # Get calculated values from solver result
                                                                # The solver doesn't modify input objects, so we need to calculate these
                                                                plate_h = solver.equations.calculate_plate_height(n_vert, pitch, Lev)
                                                                plate_l = solver.equations.calculate_plate_length(
                                                                    a, n_rows, gage, Leh,
                                                                    col_flange_width, col_web_thk
                                                                )
                                                                
                                                                cand = {
                                                                    'Vu': Vu, 't': t, 'rows': n_rows, 'd': d, 'n_vert': n_vert,
                                                                    'depth': depth, 'flange_thk': flange, 'web_t': web_t,
                                                                    'grade': grade, 'UC': uc_max, 'UC_name': uc_name,
                                                                    'pitch': pitch, 'gage': gage,
                                                                    'Lev': Lev, 'Leh': Leh, 'a': a,
                                                                    'plate_h': plate_h, 'plate_l': plate_l,
                                                                    'weld_leg': weld_leg, 'weld_grade': weld_Fe,
                                                                    'col_flange_width': col_flange_width,
                                                                    'col_web_thk': col_web_thk,
                                                                    'beam_section': b_lbl, 'column_section': c_lbl,
                                                                    # Add individual UC values
                                                                    'UC_bolt_plate_side': uc_values.get('UC_bolt_plate_side'),
                                                                    'UC_bolt_web_side': uc_values.get('UC_bolt_web_side'),
                                                                    'UC_plate_net_shear_rupture': uc_values.get('UC_plate_net_shear_rupture'),
                                                                    'UC_plate_block_shear': uc_values.get('UC_plate_block_shear'),
                                                                    'UC_weld_strength': uc_values.get('UC_weld_strength'),
                                                                    'UC_plate_gross_shear_yield': uc_values.get('UC_plate_gross_shear_yield'),
                                                                    'UC_max_plate_thickness': uc_values.get('UC_max_plate_thickness'),
                                                                    'UC_support_at_weld': uc_values.get('UC_support_at_weld'),
                                                                    'UC_flexural_yield_local_buckling': uc_values.get('UC_flexural_yield_local_buckling'),
                                                                    'UC_flexural_rupture': uc_values.get('UC_flexural_rupture'),
                                                                    'UC_shear_flexural_interaction': uc_values.get('UC_shear_flexural_interaction')
                                                                }
                                                                weights_info = result['result'].get('weights') if result['result'] else None
                                                                if weights_info and weights_info.get('total_lb') is not None:
                                                                    cand['material_weight_lb'] = weights_info.get('total_lb')
                                                                    cand['weight_plate_lb'] = weights_info.get('plate_lb')
                                                                    cand['weight_weld_lb'] = weights_info.get('weld_lb')
                                                                    cand['weight_bolt_lb'] = weights_info.get('bolt_lb')
                                                                # old approximate weight calculation (candidate_material_weight)
                                                                group_candidates.append(cand)
                                                    except Exception:
                                                        # skip failures silently
                                                        pass
        
        # CASCADE ALGORITHM: Find optimal target configuration
        # Priority: 1) Perfect match (UC 0.5-0.95), 2) Minimum legal config, 3) Lightest feasible
        final_configs = []
        if group_candidates:
            # Get all feasible configurations
            feasible = [g for g in group_candidates if g['UC'] is not None and g['UC'] <= 1.0]
            
            if feasible:
                # Sort by weight (lightest first) for cascade algorithm
                feasible_sorted = sorted(feasible, key=lambda x: x['material_weight_lb'])
                
                # Helper function to check if config is minimum geometric
                def is_minimum_geometric_config(cand):
                    """Check if this is the minimum legal geometric configuration."""
                    # Minimum requirements based on our parameter ranges:
                    min_bolts = min(n_verts)      # Minimum bolt count (2)
                    min_rows = min(n_rows_list)   # Minimum rows (1)
                    min_plate_t = min(plate_ts)   # Minimum plate thickness (0.25")
                    min_weld = min(weld_legs)     # Minimum weld (0.1875")
                    min_bolt_d = min(bolt_ds)     # Minimum bolt diameter (0.5")
                    
                    is_min = (
                        cand['n_vert'] == min_bolts and
                        cand['rows'] == min_rows and
                        cand['t'] == min_plate_t and
                        cand['weld_leg'] == min_weld and
                        cand['d'] == min_bolt_d
                    )
                    return is_min
                
                target_config = None
                minimum_config = None
                perfect_match_found = False
                
                # CASCADE: Iterate lightest to heaviest
                for cand in feasible_sorted:
                    uc = cand['UC']
                    
                    # IF UC is in optimal range (0.5 to 0.95): PERFECT MATCH - Stop and save
                    if MIN_UC <= uc <= MAX_UC:
                        target_config = cand
                        target_config['optimized'] = 1
                        target_config['optimization_status'] = "OPTIMIZED"
                        perfect_match_found = True
                        break  # Stop searching - found perfect match
                    
                    # IF UC < MIN_UC: Check if minimum geometric config
                    elif uc < MIN_UC:
                        if is_minimum_geometric_config(cand) and minimum_config is None:
                            # This is the minimum legal config - remember it
                            minimum_config = cand
                            # Don't break - continue to see if we find perfect match
                    
                    # IF UC > MAX_UC but <= 1.0: Keep as fallback (feasible but not optimal)
                    # Continue searching for perfect match
                
                # Determine final target based on cascade results
                if perfect_match_found:
                    # Perfect match found - already set
                    pass
                elif minimum_config is not None:
                    # No perfect match, but found minimum legal config - use it
                    target_config = minimum_config
                    target_config['optimized'] = 1
                    target_config['optimization_status'] = "MINIMUM_LEGAL"
                else:
                    # No perfect match, no minimum config - use lightest feasible
                    target_config = feasible_sorted[0]
                    target_config['optimized'] = 1
                    if target_config['UC'] > MAX_UC:
                        target_config['optimization_status'] = "FEASIBLE"
                    else:  # UC < MIN_UC but not minimum geometric
                        target_config['optimization_status'] = "NON_OPTIMIZED"
                
                # Save ONLY the target config
                final_configs = [target_config]

        return {
            'bi': bi,
            'ci': ci,
            'Vu': Vu,
            'vu_attempt': vu_attempt,
            'vu_success': vu_success,
            'feasible_configs': final_configs
        }

    # ---------------------------
    # Main loop (flowchart order) - MULTIPROCESSING VERSION
    # ---------------------------
    def generate_connection_data(self, output_file: str = 'shear_tab_major_db_modular_varied.csv'):
        fieldnames = [
            'Vu', 't', 'rows', 'd', 'n_vert', 'depth', 'flange_thk', 'web_t', 'grade',
            'UC', 'UC_name',
            'pitch', 'gage', 'Lev', 'Leh', 'a', 'plate_h', 'plate_l', 'weld_leg', 'weld_grade',
            'col_flange_width', 'col_web_thk', 'beam_section', 'column_section',
            'material_weight_lb', 'weight_plate_lb', 'weight_weld_lb', 'weight_bolt_lb',
            'optimized', 'optimization_status',
            # Individual strength checks
            'UC_bolt_plate_side', 'UC_bolt_web_side', 'UC_plate_net_shear_rupture',
            'UC_plate_block_shear', 'UC_weld_strength', 'UC_plate_gross_shear_yield',
            'UC_max_plate_thickness', 'UC_support_at_weld', 'UC_flexural_yield_local_buckling',
            'UC_flexural_rupture', 'UC_shear_flexural_interaction'
        ]

        print("="*80)
        print("MULTIPROCESSING DATA GENERATION (MINOR AXIS)")
        print("="*80)
        print("Starting: BEAM (random) -> COLUMN (random) -> ratio checks -> Vu -> configs -> CASCADE ALGORITHM")
        print(f"Output: {output_file}")
        print(f"Processes: {self.n_processes}")
        print(f"Optimization band: {self.MIN_UC}-{self.MAX_UC}")
        print(f"Upper-bound combos: {self.total_combinations:,}")
        print(f"Strategy: CASCADE ALGORITHM - Save 1 optimal config per (Vu, beam, column)")
        print(f"  Priority 1: Perfect match (UC {self.MIN_UC}-{self.MAX_UC}) - lightest in range")
        print(f"  Priority 2: Minimum legal config (even if UC < {self.MIN_UC})")
        print(f"  Priority 3: Lightest feasible (if no perfect match or minimum found)")
        print(f"Total Vu values: {len(self.Vu_values)} ({min(self.Vu_values)}-{max(self.Vu_values)} kips)")
        print("="*80 + "\n")

        self.start_time = time.time()
        total_success = 0
        total_attempt = 0
        status_counts = {}
        
        # Prepare all work items (pair, Vu combinations)
        work_items = []
        for bi, ci in self.valid_pairs:
            beam = self.beam_sections.loc[bi]
            col = self.column_sections.loc[ci]
            
            # Convert to dict for serialization
            beam_dict = beam.to_dict()
            col_dict = col.to_dict()
            
            for Vu in self.Vu_values:
                work_items.append((
                    bi, ci, Vu, beam_dict, col_dict,
                    self.mat_plate, self.mat_beam, self.mat_bolt, self.mat_column,
                    self.bolt_ds, self.n_verts, self.n_rows_list, self.plate_ts, self.weld_legs,
                    self.pitch_values, self.gage_values, self.Lev_values, self.Leh_values, self.a_values,
                    self.MIN_UC, self.MAX_UC, self.STEEL_DENS, self.ASSUMED_BOLT_LENGTH
                ))
        
        total_work_items = len(work_items)
        print(f"Total work items to process: {total_work_items:,}")
        print(f"(Each work item = one beam-column pair x Vu combination)\n")
        
        # Open CSV file for writing
        with open(output_file, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            # Process work items using multiprocessing
            completed_items = 0
            
            with Pool(processes=self.n_processes) as pool:
                # Use imap_unordered for better performance (results arrive as they complete)
                for result in pool.imap_unordered(self._process_single_pair_vu, work_items, chunksize=1):
                    completed_items += 1
                    
                    # Update statistics
                    total_attempt += result['vu_attempt']
                    total_success += result['vu_success']
                    
                    # Write feasible configurations to CSV
                    for row in result['feasible_configs']:
                        writer.writerow(row)
                        status = row.get('optimization_status', 'UNKNOWN')
                        status_counts[status] = status_counts.get(status, 0) + 1
                    
                    # Progress update
                    if completed_items % 10 == 0 or completed_items == total_work_items:
                        elapsed = time.time() - self.start_time
                        pct = (completed_items / total_work_items) * 100
                        rate = completed_items / elapsed if elapsed > 0 else 0.0
                        eta = (elapsed / completed_items) * (total_work_items - completed_items) if completed_items > 0 else 0.0
                        eta_str = self._format_time(eta) if eta > 0 else "Unknown"
                        
                        total_configs_written = sum(status_counts.values())
                        print(f"\r  Progress: {pct:5.1f}% | Items: {completed_items:,}/{total_work_items:,} | "
                              f"Configs written: {total_configs_written:,} | "
                              f"Rate: {rate:5.1f} items/s | Elapsed: {self._format_time(elapsed)} | ETA: {eta_str}",
                              end="", flush=True)
            
            print()  # New line after progress updates

        # Summary
        elapsed = time.time() - self.start_time
        total_configs_written = sum(status_counts.values())
        print("\n" + "="*80)
        print("DATA GENERATION COMPLETED (MINOR AXIS)")
        print("="*80)
        print(f"Processes used:      {self.n_processes}")
        print(f"Total attempted:     {total_attempt:,}")
        print(f"Feasible (UC<=1):    {total_success:,}")
        print(f"Optimized written ({self.MIN_UC}-{self.MAX_UC}): {total_configs_written:,}")
        if total_attempt:
            print(f"Optimization rate:   {100.0*total_configs_written/max(1,total_attempt):.2f}%")
        print(f"Total time:          {self._format_time(elapsed)}")
        if elapsed > 0:
            print(f"Avg rate:            {total_attempt/elapsed:.1f} comb/s")
            print(f"Speedup estimate:    ~{self.n_processes:.1f}x vs single-threaded")
        print("\nOptimization status breakdown:")
        for k, v in sorted(status_counts.items()):
            print(f"  {k}: {v:,}")
        print(f"\nDatabase written to: {output_file}")
        print("="*80)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Shear Tab Minor Connection Data Generator (Multiprocessing)')
    parser.add_argument('--output', '-o', default='shear_tab_minor_db_modular_varied.csv',
                        help='Output CSV file name')
    parser.add_argument('--seed', type=int, default=2025, help='Random seed for beam/column order')
    parser.add_argument('--pair-limit', type=int, default=300,
                        help='Max number of random (beam, column) pairs to test')
    parser.add_argument('--processes', '-p', type=int, default=None,
                        help=f'Number of processes (default: CPU count - 1 = {max(1, cpu_count()-1)})')
    args = parser.parse_args()

    gen = ShearTabMinorDataGenerator(
        random_seed=args.seed, 
        pair_limit=args.pair_limit,
        n_processes=args.processes
    )
    gen.generate_connection_data(args.output)

if __name__ == "__main__":
    main()



