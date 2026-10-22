#!/usr/bin/env python
#
# Chess robot movement control for Lynxmotion ALD5 using AL5 inverse kinematics.
# Uses CBstate for configuration, lib_al5_2D_IK for movement, and matches interfaces for camera and chess orchestration.
# Uses pyttsx3 for text-to-speech (no .wav files).
# Now includes interactive calibration routines for all arm joints and gripper.

from __future__ import print_function
from __future__ import division

import CBstate
import sys
import time
from math import atan, sqrt
import serial
from lib_al5_2D_IK import al5_2D_IK, al5_moveMotors

import pyttsx3
engine = pyttsx3.init()
engine.setProperty('rate', 125)  # Decrease the Speed Rate

# Setup serial port from CBstate
sp = serial.Serial(CBstate.serialport, 9600)

CST_SPEED_MAX = 4000
CST_SPEED_DEFAULT = 250

AL5_DefaultPos = (1500, 1500, 1500, 500, 1500, 1500)
motors_SEWBZWrG = (90, 90, 90, 90, 90, 90)
speed_SEWBZWrG = (CST_SPEED_DEFAULT,) * 6

targetXYZGWAWR = (4, 4, 90, 90, 0, 90)

xmtrans = {
    "a": 3.5, "b": 2.5, "c": 1.5, "d": 0.5,
    "e": -0.5, "f": -1.5, "g": -2.5, "h": -3.5,
    "i": -4.5, "j": -5.5
}
xtrans = {
    "a": 0, "b": 1, "c": 2, "d": 3,
    "e": 4, "f": 5, "g": 6, "h": 7,
    "i": 8, "j": 9
}
pieceheights = {
    "p": 3.2, "r": 3.6, "n": 4.0, "b": 4.9, "q": 5.7, "k": 6.3
}
piecewidths = {
    "p": 85, "r": 81, "n": 95, "b": 80, "q": 75, "k": 77
}

cmtoin = 0.393701
squaresize = 1.37795  # inches for ALD5
rtod = 57.295779
stickout = 1.3 + 0.3 + 0.2
Gfudge = 2.9
pickheightadj = -0.27
extraheight = 1.6
extrawidth = 10  # Example fudge

graveyard = "h9"

def speaker(text):
    engine.say(text)
    engine.runAndWait()

def idleall():
    print("< Idling motors... >")
    for i in range(0, 6):
        print(("#" + str(i) + " P" + str(0) + "\r").encode())
        sp.write(("#" + str(i) + " P" + str(0) + "\r").encode())
    print("< Done >")

def armangle(squarename):
    x = xmtrans[squarename[0:1]] * squaresize
    y = (8 - int(squarename[1:2])) * squaresize
    return (Gfudge + 90 + (rtod * (atan(x / (y + 3.425)))))

def armlength(squarename):
    x = xmtrans[squarename[0:1]] * squaresize
    y = (8 - int(squarename[1:2])) * squaresize
    return (((sqrt(x ** 2 + (y + 3.425) ** 2)) + stickout) * 0.84)

def adjforlen(armlen):
    if armlen < 4:
        return 0.2
    elif armlen < 7:
        return 0
    elif armlen < 11:
        return -0.2
    else:
        return 0

def movemotors(targetXYZGWAWR):
    errorValue = al5_2D_IK(targetXYZGWAWR)
    if isinstance(errorValue, tuple):
        motors_SEWBZWrG = errorValue
    else:
        print(errorValue)
        motors_SEWBZWrG = (90, 90, 90, 90, 90, 90)
    al5_moveMotors(motors_SEWBZWrG, speed_SEWBZWrG, sp)
    time.sleep(2)

def opengripper():
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1], targetXYZGWAWR[2], 42 + extrawidth, targetXYZGWAWR[4], targetXYZGWAWR[5])
    movemotors(targetXYZGWAWR)

def closegripper(piecetype):
    global targetXYZGWAWR
    jawsize = piecewidths.get(piecetype, 85) + 15 + extrawidth
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1], targetXYZGWAWR[2], jawsize, targetXYZGWAWR[4], targetXYZGWAWR[5])
    movemotors(targetXYZGWAWR)
    time.sleep(1.4)

def moveXYZGWA(X, Y, Z, G, WA):
    global targetXYZGWAWR
    targetXYZGWAWR = (float(X), float(Y), float(Z), float(G), float(WA), targetXYZGWAWR[5])
    movemotors(targetXYZGWAWR)

def movearm(X, Z):
    global targetXYZGWAWR
    targetXYZGWAWR = (float(X), targetXYZGWAWR[1], float(Z), targetXYZGWAWR[3], targetXYZGWAWR[4], targetXYZGWAWR[5])
    movemotors(targetXYZGWAWR)

def moveXY(toheight):
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], float(toheight), targetXYZGWAWR[2], targetXYZGWAWR[3], -90, targetXYZGWAWR[5])
    movemotors(targetXYZGWAWR)

def pickuppiece(pickheight, piecetype, armlen):
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1], targetXYZGWAWR[2], targetXYZGWAWR[3], -75, targetXYZGWAWR[5])
    movemotors(targetXYZGWAWR)
    moveXY(pickheight + extraheight + adjforlen(armlen))
    closegripper(piecetype)
    moveXY(6)

def droppiece(dropheight, armlen):
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1], targetXYZGWAWR[2], targetXYZGWAWR[3], -75, targetXYZGWAWR[5])
    movemotors(targetXYZGWAWR)
    moveXY(dropheight + extraheight + adjforlen(armlen))
    opengripper()
    moveXY(5 + extraheight)

def takepiece(X, Z, targetpiece):
    Xgraveyard = armlength(graveyard) + 0.5
    Zgraveyard = armangle(graveyard)
    movearm(X, Z)
    tpickheight = (pieceheights.get(targetpiece.lower(), 3.2) * cmtoin) + pickheightadj
    pickuppiece(tpickheight, targetpiece, X)
    movearm(Xgraveyard, Zgraveyard)
    time.sleep(1.5)
    droppiece(tpickheight, Xgraveyard)
    gohome()

def iscastling(sourcesquarename):
    if CBstate.cbstate == 4:
        rsourceX = armlength("h8")
        rsourceZ = armangle("h8")
        rtargetX = armlength("f8")
        rtargetZ = armangle("f8")
    elif CBstate.cbstate == 5:
        rsourceX = armlength("a8")
        rsourceZ = armangle("a8")
        rtargetX = armlength("d8")
        rtargetZ = armangle("d8")
    else:
        return
    print("Castling " + sourcesquarename)
    movearm(rsourceX, rsourceZ)
    pickrheight = (pieceheights["r"] * cmtoin) + pickheightadj
    pickuppiece(pickrheight, "r", rsourceX)
    movearm(rtargetX, rtargetZ)
    droppiece(pickrheight - 0.1, rtargetX)
    gohome()

def enpassant(sourcesquarename):
    if CBstate.cbstate == 2:
        epsquarename = sourcesquarename[0:1] + str(int(sourcesquarename[1:2]) - 1)
        takepiece(armlength(epsquarename), armangle(epsquarename), 'p')

def updateboard(source, target, boardbefore):
    sourcex = xtrans[source[0]]
    sourcey = 8 - int(source[1])
    targetx = xtrans[target[0]]
    targety = 8 - int(target[1])
    boardbefore[targety][targetx] = boardbefore[sourcey][sourcex]
    boardbefore[sourcey][sourcex] = "."
    return boardbefore

def movepiece(sourcesquarename, targetsquarename, boardbefore):
    sourceX = armlength(sourcesquarename)
    sourceZ = armangle(sourcesquarename)
    targetX = armlength(targetsquarename)
    targetZ = armangle(targetsquarename)

    sourcex = xtrans[sourcesquarename[0]]
    sourcey = 8 - int(sourcesquarename[1])
    targetx = xtrans[targetsquarename[0]]
    targety = 8 - int(targetsquarename[1])

    if boardbefore[targety][targetx] != ".":
        print("Take piece!")
        takepiece(targetX, targetZ, boardbefore[targety][targetx].lower())

    movearm(sourceX, sourceZ)
    sourcepiece = boardbefore[sourcey][sourcex].lower()
    pickheight = (pieceheights.get(sourcepiece, 3.2) * cmtoin) + pickheightadj
    pickuppiece(pickheight, sourcepiece, sourceX)

    movearm(targetX, targetZ)
    droppiece(pickheight - 0.1, targetX)
    gohome()

    iscastling(sourcesquarename)
    enpassant(sourcesquarename)

def calibrategripper():
    global targetXYZGWAWR
    speaker("Gripper calibration. Enter angle in degrees, or q to quit.")
    while True:
        angle = input("Provide gripper angle in degrees, or q to finish: ")
        if angle == "q":
            break
        try:
            targetXYZGWAWR = (
                targetXYZGWAWR[0],
                targetXYZGWAWR[1],
                targetXYZGWAWR[2],
                float(angle),
                targetXYZGWAWR[4],
                targetXYZGWAWR[5],
            )
            movemotors(targetXYZGWAWR)
            speaker(f"Moved gripper to {angle} degrees.")
        except Exception as e:
            print(f"Error: {e}")

def calibrate_arm():
    speaker("Arm calibration. You can calibrate Shoulder, Elbow, Wrist angle, Base rotation, Gripper, or Wrist rotate.")
    joints = [
        ("Shoulder", 1),
        ("Elbow", 2),
        ("Wrist angle", 3),
        ("Base rotation", 0),
        ("Gripper", 4),
        ("Wrist rotate", 5)
    ]
    while True:
        print("\nCalibration Menu:")
        for idx, (name, joint) in enumerate(joints):
            print(f"{idx+1}: {name}")
        print("q: Quit calibration")
        choice = input("Select joint to calibrate (number), or q: ")
        if choice == "q":
            speaker("Finished arm calibration.")
            break
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(joints):
                print("Invalid joint number.")
                continue
            joint_name, joint_idx = joints[idx]
            speaker(f"Calibrating {joint_name}. Enter angle in degrees to move, or q to return to menu.")
            while True:
                angle = input(f"Enter angle for {joint_name}, or q: ")
                if angle == "q":
                    break
                try:
                    # Set target for selected joint, keep others fixed
                    motors = list(motors_SEWBZWrG)
                    motors[joint_idx] = float(angle)
                    # For shoulder and elbow, use IK if needed, else direct value
                    if joint_name in ["Shoulder", "Elbow", "Wrist angle", "Base rotation", "Gripper", "Wrist rotate"]:
                        # Move the specified joint, keep others as is
                        pulses = list(AL5_DefaultPos)
                        # Map joint index to servo number order as needed
                        # Base rotation: 0, Shoulder: 1, Elbow: 2, Wrist angle: 3, Gripper: 4, Wrist rotate: 5
                        pulses[joint_idx] = int(angle)
                        # Send command to the servo directly
                        sp.write(("#" + str(joint_idx) + " P" + str(pulses[joint_idx]) + " S" + str(CST_SPEED_DEFAULT) + "\r").encode())
                        time.sleep(1)
                        speaker(f"Moved {joint_name} to {angle} degrees.")
                except Exception as e:
                    print(f"Error: {e}")
        except Exception as e:
            print(f"Error: {e}")

def gohome():
    try:
        moveXYZGWA(2.5, 7.0, 90 + Gfudge, 42 + extrawidth, -110)
    except KeyboardInterrupt:
        quitter()

def init(calibrate=False):
    sp.write(("#0 PO99\r").encode())  # offset base
    sp.write(("#1 PO-60\r").encode())  # offset shoulder
    sp.write(("#2 PO-29\r").encode())  # offset elbow
    sp.write(("#3 PO85\r").encode())  # offset wrist
    print("Default position is " + str(AL5_DefaultPos) + ".")
    for i in range(0, 6):
        print(("#" + str(i) + " P" + str(AL5_DefaultPos[i]) + "\r").encode())
        sp.write(("#" + str(i) + " P" + str(AL5_DefaultPos[i]) + "\r").encode())
        time.sleep(0.3)
    time.sleep(0.7)
    gohome()
    if calibrate:
        calibrate_arm()
        calibrategripper()
        gohome()

def quitter():
    print("Game ends")
    idleall()
    time.sleep(3)
    engine.stop()
    sys.exit()

# Optionally: nudgespecial, park, other features can be added as needed.
