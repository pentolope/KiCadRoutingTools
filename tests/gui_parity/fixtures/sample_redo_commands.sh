#!/bin/bash
# Minimal self-contained manifest fixture for test_manifest_plan_parity.py.
# The converter only PARSES commands (never runs them), so the referenced
# boards need not exist. Exercises every flag the parity gate asserts:
# clearance/track-width/via/grid/max-ripup/hole-to-hole/no-bga-zones/
# no-gnd-vias/escape-method/diff-pair-gap/plane-layers/
# rip-existing-nets/protect-nets (#521)/fab-tier/fab-overrides (#237 -- the
# ONLY asserted manifest carrying --fab-overrides, so this fixture is what
# keeps its loss detectable; the corpus never uses it).
# The route_diff.py step names --no-bga-zones refs (only those components'
# zones), where the route.py step keeps the bare flag (every zone). The
# bga_fanout, route_diff and route_planes steps carry the flags the flag
# enumeration found reaching nothing on those tools (--plane-net-layers,
# --layer-costs, --check-for-previous, --diff-pair-intra-match, ...).
# The route.py step also carries --bus (one corpus board records it, and the
# gate never asserted it), the flags whose control has another name
# (--can-swap-to-top-layer, --skip-routing), the --no-X flags that must
# UNTICK a positive checkbox, two --length-match-group occurrences (each must
# survive as its own group), and the keepout / guide-corridor switches with
# non-default layers and spacing, and --route-first's ordered patterns.
# --relax-drc-severities sits right before
# --clearance: the converter once read that switch as a value flag and
# swallowed the token after it. Added to the existing route.py line rather
# than as a new command: tests/test_506_507 pins the step sequence.
set -e
# cwd=/repo
python3 -u -X utf8 bga_fanout.py board.kicad_pcb --output step1.kicad_pcb --component U1 --nets '*' '!GND' --clearance 0.09 --track-width 0.0762 --via-size 0.25 --via-drill 0.15 --grid-step 0.05 --escape-method auto --plane-net-layers GND:In1.Cu,In4.Cu +3V3:In2.Cu --check-for-previous --no-inner-top-layer --force-escape-direction --layer-costs 1 5 --diff-pairs '*CK*' '*DQS*' --diff-pair-gap 0.1143
# cwd=/repo
python3 -u -X utf8 route_diff.py step1.kicad_pcb step2.kicad_pcb --nets /USB/D+ /USB/D- --clearance 0.10 --diff-pair-gap 0.1 --via-size 0.45 --via-drill 0.2 --grid-step 0.05 --no-gnd-vias --no-bga-zones U7 U9 --diff-pair-intra-match --ac-couple-match --diff-chamfer-extra 2
# cwd=/repo
python3 -u -X utf8 route.py step2.kicad_pcb step3.kicad_pcb --nets '*' '!GND' '!+3V3' --no-bga-zones --relax-drc-severities --clearance 0.09 --track-width 0.0762 --via-size 0.45 --via-drill 0.2 --hole-to-hole-clearance 0.2 --grid-step 0.05 --max-ripup 10 --max-iterations 1000000 --fab-tier advanced --fab-overrides my_fab.txt --ordering bus --route-first /CLK '/DATA*' --bus --bus-detection-radius 4 --can-swap-to-top-layer --skip-routing --no-smoothing --no-stub-layer-swap --no-power-tap-neckdown --length-match-group 'DDR_DQ*' 'DDR_DQS*' --length-match-group auto --keepout --keepout-layer User.3 --guide-corridor --guide-corridor-layer User.4 --guide-corridor-spacing 0.5 --no-fix-drc-settings
# cwd=/repo
python3 -u -X utf8 route_planes.py step3.kicad_pcb step4.kicad_pcb --nets GND +3V3 --plane-layers In1.Cu In4.Cu --layer-costs 1 5 --via-size 0.45 --via-drill 0.2 --track-width 0.09 --clearance 0.10 --hole-to-hole-clearance 0.2 --grid-step 0.05
# cwd=/repo
python3 -u -X utf8 route_disconnected_planes.py step4.kicad_pcb step5.kicad_pcb --nets GND +3V3 --clearance 0.09 --via-size 0.25 --via-drill 0.15 --track-width 0.0762 --grid-step 0.025 --hole-to-hole-clearance 0.2 --rip-blocker-nets
