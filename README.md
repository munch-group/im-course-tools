# im-course-tools

The `im` command for the [Instructing Machines](https://munch-group.org/instructing-machines)
course: a small, pure-Python CLI that students run from their course folder.

```bash
im check                 # is my environment working?
im doctor                # why is it not working?
im get iteration         # download a chapter notebook
im get alignmentproject  # download a whole project
im get                   # everything on offer
im update                # refresh the environment from the website
```

## Why it is a package

The course folder students download holds their own work and nothing else. The
four commands used to be four Python files copied into that folder, which put
code students had no reason to read next to the notebooks they did, and left no
way to fix a bug for a hundred people already holding a copy. Here they travel
with the environment instead: a fix reaches everyone through a release.

## What the commands guarantee

Nothing ever overwrites a student's work.

- A notebook that already exists is left exactly as it is, and the fresh copy
  lands beside it as `iteration-2.ipynb`.
- A project that already exists stops the command. A project is a folder worked
  in for a week, and unpacking over it would put the empty starting file back on
  top of real code.
- A project zip is checked before it is unpacked: every entry must live inside
  the project's own folder, so an archive naming `../../somewhere` writes
  nothing.
- `im update` downloads both `pixi.toml` and `pixi.lock`, checks each is what it
  claims to be, and keeps a `.backup` of the old one before writing.

Files land in the course folder, found by walking up from wherever the student
happens to be, so `im get` works two subfolders deep.

## When something is wrong

`im check` answers one question: are the packages there. `im doctor` answers the
question a student actually has, which is why they are not, and it is the one
command that runs anywhere rather than only in the course folder — because
being in the wrong folder is one of the things it is there to notice.

It reads the machine and changes nothing on it, so it is always safe to tell a
hundred people to run it. It looks at, in this order:

- **This machine** — which OS and Python, and on a Mac whether the terminal is
  the Intel one being emulated, which quietly gets the wrong build of everything.
- **The course folder** — whether there is one, and where it probably is if not;
  whether it is inside OneDrive or iCloud, which will fight pixi over tens of
  thousands of small files; letters in the path that the tools underneath pixi
  mishandle; the Windows 260-character path limit; a network or removable drive;
  whether anything can be written there at all; and whether there is room.
- **pixi** — installed, and whether *this terminal* can see it, which is a
  different question and a much shorter fix.
- **The environment** — built or not, its lock file current, every course
  package importable *in that environment* rather than in whichever Python is
  running `im`, and whether the `im` being run is the one inside it.
- **Security software** — on Windows by asking Windows' own Security Center,
  on macOS by looking where the dozen products a university laptop carries
  install themselves.
- **Internet access** — a TLS connection to every host pixi downloads from, and
  then the part that matters: who signed each certificate. Antivirus that
  inspects encrypted traffic substitutes its own, which pixi refuses and a
  browser accepts, and that gap is the single most common reason an install
  fails on a laptop that browses the web perfectly well. Also proxy and
  certificate variables set in the terminal, and a clock wrong enough to make
  valid certificates look expired.
- **VS Code** — installed, with the Python and Jupyter extensions.

Every problem is printed twice: once as a line in the scan, and once at the
bottom with the command or click-path that fixes it, written out in full,
because a student reading it is by definition having trouble reaching the
website.

```bash
im doctor                # the whole thing
im doctor --offline      # skip the network checks
im doctor --report       # also write im-doctor-report.txt to send to an instructor
```

Warnings do not set the exit code; only failures do.

### Installing it outside the course environment

`im doctor` is most useful on a machine where the course environment is exactly
what is broken, so it is worth having a copy that does not depend on one:

```bash
pixi global install -c conda-forge -c munch-group im-course-tools
```

A globally installed `im` notices that it is the global one and says so, since
`im check` can only see the packages in the Python it is itself running on and
would otherwise report a working environment as empty.

## Development

```bash
pixi run install-dev     # editable install into the pixi environment
pixi run test            # the test suite
```

The tests run against a fake course website on disk, reached through a `file://`
URL, so they never touch the real site. `IM_COURSE_URL` and `IM_COURSE_FOLDER`
are the two overrides they use, and they work by hand too:

```bash
IM_COURSE_URL=file:///path/to/_book im get iteration
```

## Release

```bash
pixi run release         # bump, tag, and push; CI builds the conda and pip packages
```

## License

MIT
