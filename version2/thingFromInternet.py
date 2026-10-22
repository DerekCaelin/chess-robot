import serial
import time

# Check your /dev/ttyUSBn and baud rate
# Adjust as needed (e.g., '/dev/ttyUSB0', 115200)
serial_port = '/dev/ttyUSB0' 
baud_rate = 9600

# Function to send a command to the arm
def send_command(ser, command):
    ser.write(command.encode('ascii') + b'\r') # Add carriage return for SSC-32U
    time.sleep(0.1)

try:
    # Open the serial port
    ser = serial.Serial(serial_port, baud_rate)
    time.sleep(2)  # Wait for the serial connection to initialize

    print(f"Connected to {serial_port} at {baud_rate} baud.")

    # 1. Check firmware version
   # send_command(ser, "VER")
   # version = ser.readline().strip()
   # print(f"SSC-32U Version: {version.decode('ascii')}")

    # 2. Command a servo to move (e.g., servo channel 0 to center position)
    print("Moving servo 0 to position 1500 (center)...")
    send_command(ser, "#0 P1500 T1000") # Channel 0, 1500 pulse width, 1000ms duration
    time.sleep(1.5) # Wait for the move to complete

    # 3. Move the servo again
    print("Moving servo 0 to position 2000...")
    send_command(ser, "#0 P2000 T1000")
    time.sleep(1.5)

    # 4. Return to center
    print("Moving servo 0 back to position 1500...")
    send_command(ser, "#0 P1500 T1000")
    time.sleep(1.5)

    print("Program finished.")

finally:
    if ser.is_open:
        ser.close()
        print("Serial port closed.")

