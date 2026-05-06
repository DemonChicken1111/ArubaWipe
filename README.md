# Aruba AP Batch Wiper

This python script allows you to wipe aruba AP's in batches over console connection. This utility should work on Linux and Windows, and is untested on Mac. The program will detect any serial connections present (Tested on a KEYSPAN 19-HS) and will automatically grab and store the serial number and MAC address. It will then factory reset the device(s), save the configuration, and prompt you to move the console cable to a new device. 

The program will prompt you for the number of batches you would like to do, this number determines the number of times the program will scan over all the serial connections (you can have multiple at one time) and wipe the devices. 

The global timeout is 5 minutes per Acess point, and 1 minute for each state (ie. waiting for boot, waiting for factory reset) if you find that these are too low you can change them in the script under the configuration variables.
