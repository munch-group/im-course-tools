"""`python -m im_course_tools`, for reaching this copy without going through PATH.

The upgrade runs the command again as a process of its own, and the copy that
has to run is the one just installed rather than whichever `im` a PATH happens
to name. Where the console script cannot be found beside the interpreter, this
is how the same interpreter is asked for it by name.
"""

from .cli import main

if __name__ == "__main__":
    main()
