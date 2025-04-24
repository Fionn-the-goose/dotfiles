#!/bin/sh

TABLET_STYLUS=`xinput | grep "Tablet Monitor Pen Pen" | sed -e "s/.*id=\(..\).*/\1/g"`
TABLET=`xinput | grep 'Tablet Monitor Pad pad' | sed -e "s/.*id=\(..\).*/\1/g"`i

#xrandr --output DVI-D-0 --off

xrandr --dpi 96
xinput map-to-output $TABLET_STYLUS DisplayPort-1
xinput set-prop $TABLET_STYLUS "Wacom Tablet Area" -89, 193, 58641, 32793

