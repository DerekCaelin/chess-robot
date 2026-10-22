# CBint.py - command-line entry point (kept alive for headless/debug use).
#
# All game logic now lives in chessapp.game_controller (shared with the
# touchscreen UI in ui/app.py). This module just wires it to the console:
# startup calibration prompts, arm init, and the hands-free play loop.
#
# Credit to chess.fortherapy.co.uk for much of the original code here.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chessapp import settings
settings.apply()          # must run before CBstate consumers read their consts

import CBstate
from chessapp.game_controller import ChessGame
import LEGO_Chess_rpd as RD
import playermove_rpd as RDpm


def startup_calibration():
    points_path = CBstate.mydir + "points.txt"
    if not os.path.exists(points_path):
        print("No board calibration found.")
        do_cal = True
    else:
        do_cal = input("Recalibrate board corners? (y/n): ").strip().lower() == "y"
    if do_cal:
        print("Running automatic board detection (live camera).")
        import live_calibrate
        live_calibrate.run_live()
        if not os.path.exists(points_path):
            print("Auto-detect did not save a calibration — opening manual "
                  "corner-click tool.")
            import board_calibrate
            board_calibrate.run_calibration()
    else:
        import piece_detect
        drift = piece_detect.validate_calibration()
        if drift is None:
            print("Could not validate board corners (board unreadable).")
        elif drift > piece_detect.DRIFT_LIMIT:
            print("WARNING: board drifted %.1f px since calibration. "
                  "Re-calibrating..." % drift)
            import live_calibrate
            live_calibrate.run_live()
        else:
            print("Board calibration verified (drift %.1f px)." % drift)


def main():
    RD.speaker("Hello! Let's play chess!")
    print("\nChess Program\n")
    game = ChessGame()
    game.new_game()
    startup_calibration()

    while True:
        if RD.init():
            break
        print("Arm not connected. Check the SSC-32U USB cable and its servo "
              "power (VIN LED), then press Enter to retry.")
        input()

    input("Set up the pieces, then press Enter to begin.")

    while not game.game_over:
        RD.speaker("Your move.")
        bmessage = game.wait_player_move()
        if bmessage == "JOSTLE":
            if game.handle_jostle():
                print("Recalibrated. Please make your move again.")
            else:
                print("Could not auto-recalibrate. Re-run live calibration, "
                      "then press Enter.")
                input()
            continue
        if not bmessage or not bmessage[1:5].strip():
            continue
        result = game.submit_player_move(bmessage)
        if result["error"]:
            print("Move error:", result["error"])
        elif result["computer_move"]:
            print("moves:", game.movelist)
        if result["game_over"]:
            break

    print("Game over:", game.result_text or "")
    RD.quitter()


# legacy hooks referenced by old notebooks/tests
def robotinit():
    return RD.init()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        RD.quitter()
