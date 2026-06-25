# AI4CFD-freecad

> AI-driven design space search with CFD simulation, built on top of [FreeCAD](https://www.freecad.org/). Automatically explores design parameters and uses physics-informed AI to find optimal geometries through hydrodynamic analysis.

---

## Table of Contents

- [Demo](#demo)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [License](#license)

---

## Demo

### Physics AI — Hydrodynamic Analysis of Pipe

https://github.com/user-attachments/assets/64764b34-4392-4d9d-af45-4846d47e28a8

### Design Space Search

<!-- Upload Design space search.mp4 via the GitHub editor to embed it here -->

---

## Features

- **Physics-informed AI** — surrogate model trained on CFD results for fast hydrodynamic prediction
- **Automated design space search** — explores geometric parameters to find optimal configurations
- **FreeCAD integration** — runs entirely within the FreeCAD environment, no external solver setup required

---

## Requirements

- [FreeCAD](https://www.freecad.org/) 0.21+
- Python 3.8+

Install Python dependencies:

```bash
pip install -r requirement.txt
```

---

## Installation

```bash
git clone https://github.com/YujiaWei2020/AI4CFD-freecad.git
cd AI4CFD-freecad
pip install -r requirement.txt
```

---

## Usage

1. Open **FreeCAD**
2. Load the macro/script from this repository
3. Configure your design parameters
4. Run the design space search — results will be visualized inside FreeCAD

### Tutorial

Step-by-step guide to creating your first digital twin system:
https://www.youtube.com/watch?v=0pIovv9IgwQ

---

## License

This project is licensed under the [MIT License](LICENSE).
