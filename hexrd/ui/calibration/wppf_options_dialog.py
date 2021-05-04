import copy
import importlib.resources
import os
from pathlib import Path

import numpy as np
import yaml

from PySide2.QtCore import Qt, QObject, QSignalBlocker, Signal
from PySide2.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QMessageBox, QSizePolicy,
    QTableWidgetItem, QWidget
)

from hexrd.material import _angstroms
from hexrd.WPPF import LeBail, Rietveld, \
    Parameters, _lpname, _rqpDict,  _getnumber, _nameU
from hexrd import constants

import hexrd.ui.resources.calibration as calibration_resources
from hexrd.ui.constants import OverlayType
from hexrd.ui.hexrd_config import HexrdConfig
from hexrd.ui.scientificspinbox import ScientificDoubleSpinBox
from hexrd.ui.select_items_dialog import SelectItemsDialog
from hexrd.ui.ui_loader import UiLoader


COLUMNS = {
    'name': 0,
    'value': 1,
    'minimum': 2,
    'maximum': 3,
    'vary': 4
}

LENGTH_SUFFIXES = ['_a', '_b', '_c']


class WppfOptionsDialog(QObject):

    accepted = Signal()
    rejected = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        loader = UiLoader()
        self.ui = loader.load_file('wppf_options_dialog.ui', parent)
        self.ui.setWindowTitle('WPPF Options Dialog')

        self.reset_initial_params()
        self.load_settings()
        self.update_extra_params()

        self.value_spinboxes = []
        self.minimum_spinboxes = []
        self.maximum_spinboxes = []
        self.vary_checkboxes = []

        self.update_gui()

        self.setup_connections()

    def setup_connections(self):
        self.ui.wppf_method.currentIndexChanged.connect(self.on_method_changed)
        self.ui.select_materials_button.pressed.connect(self.select_materials)
        self.ui.background_method.currentIndexChanged.connect(
            self.update_visible_background_parameters)
        self.ui.select_experiment_file_button.pressed.connect(
            self.select_experiment_file)
        self.ui.reset_table_to_defaults.pressed.connect(
            self.reset_table_to_defaults)
        self.ui.display_wppf_plot.toggled.connect(
            self.display_wppf_plot_toggled)

        self.ui.accepted.connect(self.accept)
        self.ui.rejected.connect(self.reject)

    def accept(self):
        try:
            self.validate()
        except Exception as e:
            QMessageBox.critical(self.ui, 'HEXRD', str(e))
            self.show()
            return

        self.save_settings()
        self.accepted.emit()

    def reject(self):
        self.rejected.emit()

    def validate(self):
        use_experiment_file = self.use_experiment_file
        if use_experiment_file and not os.path.exists(self.experiment_file):
            raise Exception(f'Experiment file, {self.experiment_file}, '
                            'does not exist')

    @property
    def method_defaults_file(self):
        return f'default_wppf_{self.wppf_method.lower()}_params.yml'

    def reset_initial_params(self):
        self.reset_default_params()
        self.params = copy.deepcopy(self.default_params)

    def reset_default_params(self):
        self._loaded_defaults_file = self.method_defaults_file
        text = importlib.resources.read_text(calibration_resources,
                                             self.method_defaults_file)
        self.default_params = yaml.load(text, Loader=yaml.FullLoader)

    def update_extra_params(self):
        # This will add extra parameters that should be there, and
        # remove extra parameters that should not.

        # First, make a deep copy of the original parameters.
        old_params = copy.deepcopy(self.params)

        # Now, reset the extra parameters.
        self.reset_extra_params()

        # Now, restore any previous settings for extra parameters
        for key in old_params:
            if key in self.default_params or key not in self.params:
                # Not an extra parameter, or the key was removed
                continue

            # Restore the previous settings
            self.params[key] = old_params[key]

    def reset_extra_params(self):
        # First, remove all extra params currently in place.
        for key in list(self.params.keys()):
            if key not in self.default_params:
                del self.params[key]

        # Now add the material parameters
        self.add_material_parameters()

    def show(self):
        self.ui.show()

    @property
    def selected_materials(self):
        if not hasattr(self, '_selected_materials'):
            # Choose the visible ones with powder overlays by default
            overlays = [x for x in HexrdConfig().overlays if x['visible']]
            overlays = [x for x in overlays if x['type'] == OverlayType.powder]
            materials = [x['material'] for x in overlays]
            self._selected_materials = list(dict.fromkeys(materials))

        return self._selected_materials

    @selected_materials.setter
    def selected_materials(self, v):
        self._selected_materials = v

    @property
    def powder_overlay_materials(self):
        overlays = HexrdConfig().overlays
        overlays = [x for x in overlays if x['type'] == OverlayType.powder]
        return list(dict.fromkeys([x['material'] for x in overlays]))

    def select_materials(self):
        materials = self.powder_overlay_materials
        selected = self.selected_materials
        items = [(name, name in selected) for name in materials]
        dialog = SelectItemsDialog(items, self.ui)
        if dialog.exec_() and self.selected_materials != dialog.selected_items:
            self.selected_materials = dialog.selected_items
            self.update_extra_params()
            self.update_table()

    def update_visible_background_parameters(self):
        is_chebyshev = self.background_method == 'chebyshev'
        chebyshev_widgets = [
            self.ui.chebyshev_polynomial_degree,
            self.ui.chebyshev_polynomial_degree_label
        ]
        for w in chebyshev_widgets:
            w.setVisible(is_chebyshev)

    @property
    def wppf_method(self):
        return self.ui.wppf_method.currentText()

    @wppf_method.setter
    def wppf_method(self, v):
        self.ui.wppf_method.setCurrentText(v)

    @property
    def refinement_steps(self):
        return self.ui.refinement_steps.value()

    @refinement_steps.setter
    def refinement_steps(self, v):
        self.ui.refinement_steps.setValue(v)

    @property
    def background_method(self):
        return self.ui.background_method.currentText()

    @background_method.setter
    def background_method(self, v):
        self.ui.background_method.setCurrentText(v)

    @property
    def chebyshev_polynomial_degree(self):
        return self.ui.chebyshev_polynomial_degree.value()

    @chebyshev_polynomial_degree.setter
    def chebyshev_polynomial_degree(self, v):
        self.ui.chebyshev_polynomial_degree.setValue(v)

    @property
    def background_method_dict(self):
        # This returns the background information in the format that
        # the WPPF classes expect in hexrd.
        method = self.background_method
        if method == 'spline':
            value = None
        elif method == 'chebyshev':
            value = self.chebyshev_polynomial_degree
        else:
            raise Exception(f'Unknown background method: {method}')

        return {method: value}

    @property
    def use_experiment_file(self):
        return self.ui.use_experiment_file.isChecked()

    @use_experiment_file.setter
    def use_experiment_file(self, b):
        self.ui.use_experiment_file.setChecked(b)

    @property
    def experiment_file(self):
        return self.ui.experiment_file.text()

    @experiment_file.setter
    def experiment_file(self, v):
        self.ui.experiment_file.setText(v)

    @property
    def display_wppf_plot(self):
        return self.ui.display_wppf_plot.isChecked()

    @display_wppf_plot.setter
    def display_wppf_plot(self, v):
        self.ui.display_wppf_plot.setChecked(v)

    def load_settings(self):
        settings = HexrdConfig().config['calibration'].get('wppf')
        if not settings:
            return

        blockers = [QSignalBlocker(w) for w in self.all_widgets]  # noqa: F841
        for k, v in settings.items():
            setattr(self, k, v)

        if self.method_was_changed():
            # Reset the default parameters if they have changed.
            self.reset_default_params()

    def save_settings(self):
        settings = HexrdConfig().config['calibration'].setdefault('wppf', {})
        keys = [
            'wppf_method',
            'refinement_steps',
            'background_method',
            'chebyshev_polynomial_degree',
            'use_experiment_file',
            'experiment_file',
            'display_wppf_plot',
            'params',
        ]
        for key in keys:
            settings[key] = getattr(self, key)

    def select_experiment_file(self):
        selected_file, _ = QFileDialog.getOpenFileName(
            self.ui, 'Select Experiment File', HexrdConfig().working_dir,
            'TXT files (*.txt)')

        if selected_file:
            path = Path(selected_file)
            HexrdConfig().working_dir = str(path.parent)
            self.ui.experiment_file.setText(selected_file)

    def reset_table_to_defaults(self):
        self.params = copy.deepcopy(self.default_params)
        self.reset_extra_params()
        self.update_table()

    def display_wppf_plot_toggled(self):
        HexrdConfig().display_wppf_plot = self.display_wppf_plot

    def create_label(self, v):
        w = QTableWidgetItem(v)
        w.setTextAlignment(Qt.AlignCenter)
        return w

    def create_spinbox(self, v):
        sb = ScientificDoubleSpinBox(self.ui.table)
        sb.setKeyboardTracking(False)
        sb.setValue(float(v))
        sb.valueChanged.connect(self.update_params)

        size_policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        sb.setSizePolicy(size_policy)
        return sb

    def create_value_spinbox(self, v):
        sb = self.create_spinbox(v)
        self.value_spinboxes.append(sb)
        return sb

    def create_minimum_spinbox(self, v):
        sb = self.create_spinbox(v)
        self.minimum_spinboxes.append(sb)
        return sb

    def create_maximum_spinbox(self, v):
        sb = self.create_spinbox(v)
        self.maximum_spinboxes.append(sb)
        return sb

    def create_vary_checkbox(self, b):
        cb = QCheckBox(self.ui.table)
        cb.setChecked(b)
        cb.toggled.connect(self.update_params)

        self.vary_checkboxes.append(cb)
        return self.create_table_widget(cb)

    def create_table_widget(self, w):
        # These are required to center the widget...
        tw = QWidget(self.ui.table)
        layout = QHBoxLayout(tw)
        layout.addWidget(w)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        return tw

    def method_was_changed(self):
        return self.method_defaults_file != self._loaded_defaults_file

    def on_method_changed(self):
        if not self.method_was_changed():
            # Didn't actually change. Nothing to do...
            return

        self.reset_initial_params()
        self.update_extra_params()
        self.update_table()

    def update_gui(self):
        blocker = QSignalBlocker(self.ui.display_wppf_plot)  # noqa: F841
        self.display_wppf_plot = HexrdConfig().display_wppf_plot

        self.update_visible_background_parameters()
        self.update_table()

    def clear_table(self):
        self.value_spinboxes.clear()
        self.minimum_spinboxes.clear()
        self.maximum_spinboxes.clear()
        self.vary_checkboxes.clear()
        self.ui.table.clearContents()

    def update_table(self):
        blocker = QSignalBlocker(self.ui.table)  # noqa: F841

        self.clear_table()
        self.ui.table.setRowCount(len(self.params))
        for i, (name, vals) in enumerate(self.params.items()):
            w = self.create_label(name)
            self.ui.table.setItem(i, COLUMNS['name'], w)

            w = self.create_value_spinbox(self.value(name, vals))
            self.ui.table.setCellWidget(i, COLUMNS['value'], w)

            w = self.create_minimum_spinbox(self.minimum(name, vals))
            self.ui.table.setCellWidget(i, COLUMNS['minimum'], w)

            w = self.create_maximum_spinbox(self.maximum(name, vals))
            self.ui.table.setCellWidget(i, COLUMNS['maximum'], w)

            w = self.create_vary_checkbox(self.vary(name, vals))
            self.ui.table.setCellWidget(i, COLUMNS['vary'], w)

    def update_params(self):
        for i, (name, vals) in enumerate(self.params.items()):
            vals[0] = self.value_spinboxes[i].value()
            vals[1] = self.minimum_spinboxes[i].value()
            vals[2] = self.maximum_spinboxes[i].value()
            vals[3] = self.vary_checkboxes[i].isChecked()

            if any(name.endswith(x) for x in LENGTH_SUFFIXES):
                # Convert from angstrom to nm for WPPF
                for j in range(3):
                    vals[j] /= 10.0

    @property
    def all_widgets(self):
        names = [
            'wppf_method',
            'refinement_steps',
            'background_method',
            'chebyshev_polynomial_degree',
            'experiment_file',
            'table',
            'display_wppf_plot',
        ]
        return [getattr(self.ui, x) for x in names]

    def convert(self, name, val):
        # Check if we need to convert this data to other units
        if any(name.endswith(x) for x in LENGTH_SUFFIXES):
            # Convert from nm to Angstroms
            return val * 10.0
        return val

    def value(self, name, vals):
        return self.convert(name, vals[0])

    def minimum(self, name, vals):
        return self.convert(name, vals[1])

    def maximum(self, name, vals):
        return self.convert(name, vals[2])

    def vary(self, name, vals):
        return vals[3]

    def create_wppf_params_object(self):
        params = Parameters()
        for name, val in self.params.items():
            kwargs = {
                'name': name,
                'value': float(val[0]),
                'lb': float(val[1]),
                'ub': float(val[2]),
                'vary': bool(val[3])
            }
            params.add(**kwargs)
        return params

    def add_material_parameters(self):
        """
        @AUTHOR:    Saransh Singh, Lawrence Livermore National Lab
        @DATE:      02/03/2021 SS 1.0 original
        @DETAILS:   a simple function to add the material parameters
        from the list of material file. this depends on which
        method i chosen. for the LeBail class the parameters
        added are the minimum set of lattice parameters. For
        the Rietveld class, the lattice parameters, fractional
        coordinates, occupancy and debye waller factors are
        added.
        """
        method = self.wppf_method

        def add_params(name, vary, value, lb, ub):
            self.params[name] = [value, lb, ub, vary]

        for x in self.selected_materials:
            mat = HexrdConfig().material(x)
            p = mat.name

            """
            add lattice parameters
            """
            lp = np.array(mat.planeData.lparms)
            rid = list(_rqpDict[mat.unitcell.latticeType][0])

            name = _lpname[rid]

            for i, (n, l) in enumerate(zip(name, lp)):
                nn = f'{p}_{n}'

                """
                first 3 are lengths, next three are angles
                """
                if rid[i] <= 2:
                    # Convert to Angstroms
                    v = l / 10
                    add_params(nn, value=v, lb=v-0.05, ub=v+0.05, vary=False)
                else:
                    add_params(nn, value=l, lb=l-1., ub=l+1., vary=False)

            # if method is LeBail
            if method == 'LeBail':
                pass

            elif method == 'Rietveld':
                """
                now adding the atom positions and
                occupancy
                """
                atom_pos = mat.unitcell.atom_pos[:, 0:3]
                occ = mat.unitcell.atom_pos[:, 3]

                atom_type = mat.unitcell.atom_type
                atom_label = _getnumber(atom_type)

                """
                now for each atom type append the fractional
                coordinates, occupation fraction and debye-waller
                factors to the list of parameters
                """
                for i in range(atom_type.shape[0]):
                    Z = atom_type[i]
                    elem = constants.ptableinverse[Z]
                    # x-coordinate
                    nn = f'{p}_{elem}{atom_label[i]}_x'
                    add_params(nn, value=atom_pos[i, 0],
                               lb=0.0, ub=1.0, vary=False)

                    # y-coordinate
                    nn = f'{p}_{elem}{atom_label[i]}_y'
                    add_params(nn, value=atom_pos[i, 1],
                               lb=0.0, ub=1.0, vary=False)

                    # z-coordinate
                    nn = f'{p}_{elem}{atom_label[i]}_z'
                    add_params(nn, value=atom_pos[i, 2],
                               lb=0.0, ub=1.0, vary=False)

                    # occupation
                    nn = f'{p}_{elem}{atom_label[i]}_occ'
                    add_params(nn, value=occ[i],
                               lb=0.0, ub=1.0, vary=False)

                    if mat.unitcell.aniU:
                        U = mat.unitcell.U
                        for j in range(6):
                            nn = f'{p}_{elem}{atom_label[i]}_{_nameU[j]}'
                            add_params(nn, value=U[i, j],
                                       lb=-1e-3, ub=np.inf, vary=False)
                    else:
                        nn = f'{p}_{elem}{atom_label[i]}_dw'
                        add_params(nn, value=mat.unitcell.U[i],
                                   lb=0.0, ub=np.inf, vary=False)

            else:
                raise Exception(f'Unknown method: {method}')

    def create_wppf_object(self):
        method = self.wppf_method
        if method == 'LeBail':
            class_type = LeBail
        elif method == 'Rietveld':
            class_type = Rietveld
        else:
            raise Exception(f'Unknown method: {method}')

        params = self.create_wppf_params_object()

        wavelength = {
            'synchrotron': [_angstroms(
                HexrdConfig().beam_wavelength), 1.0]
        }

        if self.use_experiment_file:
            expt_spectrum = np.loadtxt(self.experiment_file)
        else:
            expt_spectrum = HexrdConfig().last_azimuthal_integral_data
            # Re-format it to match the expected input format
            expt_spectrum = np.array(list(zip(*expt_spectrum)))

        phases = [HexrdConfig().material(x) for x in self.selected_materials]
        kwargs = {
            'expt_spectrum': expt_spectrum,
            'params': params,
            'phases': phases,
            'wavelength': wavelength,
            'bkgmethod': self.background_method_dict
        }

        return class_type(**kwargs)


if __name__ == '__main__':
    from PySide2.QtWidgets import QApplication

    app = QApplication()

    dialog = WppfOptionsDialog()
    dialog.ui.exec_()

    print(f'{dialog.wppf_method=}')
    print(f'{dialog.background_method=}')
    print(f'{dialog.chebyshev_polynomial_degree=}')
    print(f'{dialog.experiment_file=}')
    print(f'{dialog.params=}')
