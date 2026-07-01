# STANDARD LIBRARY IMPORTS
import sys
from pathlib import Path

path_object = Path(__file__) # give you a path object pointing to this file (conftest.py) itself.
path_object = path_object.resolve() # guarantees you get the full absolute path
path_object = path_object.parent # this is the programmatic equivalent to running "cd .."

src_path = path_object / "src" # join paths from the project root to the src folder/directory

sys.path.insert(0, str(src_path)) # this line is specially adding the src_path to the front of the list of path strings to the front so that it finds it first