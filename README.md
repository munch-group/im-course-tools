# im-course-tools

The `im` command for the [Instructing Machines](https://munch-group.org/instructing-machines)
course: a small, pure-Python CLI that students run from their course folder.

```bash
im check                 # is my environment working?
im doctor                # why is it not working?
im get iteration         # download a chapter notebook
im get alignmentproject  # download a whole project
im get                   # everything on offer
im update                # refresh the course folder from the website
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
- `im update` replaces only the six files in the course folder that belong to
  the course rather than to the student, and only the ones that actually differ
  from what the website is publishing, keeping a `.backup` of each one it does
  replace.

Files land in the course folder, found by walking up from wherever the student
happens to be, so `im get` works two subfolders deep.

## Keeping the course folder current

The folder a student downloads in week one is not only their work. It is also
the environment pixi builds, the tasks `pixi run` offers, the script that tells
VS Code where pixi lives, and VS Code's own settings — none of which they have
any reason to maintain, and every one of which is somewhere a fix eventually
has to reach. Until `im update` covered them, the only way to deliver one was
to ask a hundred people to download the folder again and move their work
across by hand, which is a thing you can ask once a term at most.

So `im update` fetches the course folder the website publishes — the same zip a
student starting today would download — and brings these up to date:

| file | what it is |
| --- | --- |
| `pixi.toml` | the environment, and the `pixi run` tasks |
| `pixi.lock` | the exact versions everyone else has |
| `.pin_pixi_path.py` | what tells VS Code where pixi lives |
| `.gitignore` | what git is to leave out |
| `.vscode/settings.json` | how VS Code finds the course Python |
| `.vscode/extensions.json` | the extensions the course asks for |

Nothing else in that download is touched: the week-one notebooks and the data
folder arrive in it too, and both are places a student works.

Out of date means *different from what is published*, compared by content
rather than by any timestamp — a clock, a fresh unzip and an editor that
rewrites line endings are each enough to make a date lie, and a file that
already matches is left alone entirely rather than replaced by an identical
copy of itself. What is replaced is kept as `<name>.backup` first, so a
student who had edited one has it back.

`pixi install` runs afterwards either way, including when nothing needed
changing: `im update` is where a student is sent when the environment is
broken, and skipping the install because the files were already right would
turn them away in exactly the case the command exists for.

## When something is wrong

`im check` answers one question: are the packages there. `im doctor` answers the
question a student actually has, which is why they are not, and it is the one
command that runs anywhere rather than only in the course folder — because
being in the wrong folder is one of the things it is there to notice.

It reads the machine and changes nothing on it, so it is always safe to tell a
hundred people to run it. The one thing it downloads goes into a temporary
cache that is thrown away again. It looks at, in this order:

- **This machine** — which OS and Python, and on a Mac whether the terminal is
  the Intel one being emulated, which quietly gets the wrong build of everything.
- **The course folder** — whether there is one, and where it probably is if not,
  including the case where it is the folder immediately inside this one, which
  is what Windows' "Extract all" leaves behind when its offered destination is
  accepted: the zip is unpacked into a new folder named after itself and already
  holds one of that name, so everything lands a level deeper than it looks and
  every command is then run in the empty half of the pair; whether it is inside
  OneDrive or iCloud, which will fight pixi over tens of thousands of small
  files; letters in the path that the tools underneath pixi mishandle; the Windows 260-character path limit; a network or removable drive;
  whether anything can be written there at all; and whether there is room.
- **pixi** — installed, and whether *this terminal* can see it, which is a
  different question and a much shorter fix.
- **Your terminal** — which shell is actually running it, asked of the process
  rather than read off `$SHELL`, because those two disagree often enough to
  matter. pixi's installer writes its PATH line into the startup file of the
  shell it was run from, and a student working in the other one can open new
  terminals all afternoon without ever seeing pixi, so "open a new terminal" is
  advice that has to be checked before it is given. Then whether that shell's
  own startup file is what puts pixi on PATH or whether it merely happens to be
  there this once, and on Windows whether PowerShell is allowed to run a script
  at all — it ships refusing to, and says so in a sentence that mentions neither
  pixi nor the course, while `pixi shell` and VS Code's own terminal activation
  are both scripts.
- **The environment** — built or not, its lock file current, whether it was
  built for the folder it is now sitting in, because a pixi environment holds
  that folder's path in hundreds of places and a moved or renamed course folder
  breaks every one of them — read from pixi's own stamp, and failing that from
  the kernel every notebook starts and the scripts pixi wrote, so that an
  environment carrying no stamp is not quietly given the benefit of the doubt;
  every course package importable *in that environment* rather than in whichever
  Python is running `im`; whether the `im` being run is the one inside it; and
  whether it is the environment this terminal is in at all, since one that is
  installed but not activated leaves `python` and `pytest` meaning whichever
  ones the machine came with — as does an Anaconda `base` that activates itself
  in every new terminal.
- **Security software** — on Windows by asking Windows' own Security Center,
  on macOS by looking where the dozen products a university laptop carries
  install themselves. Installed is not the same as at fault: nearly every
  laptop in the room carries something and nearly none of it is why an install
  failed, so what is there stays a line in the scan until the internet checks
  turn up evidence — a certificate pixi refuses, a download of pixi's own that
  never arrives — and only then does it become something to go and do.
- **Internet access** — a TLS connection to every host pixi downloads from, and
  then the part that matters: who signed each certificate. Antivirus that
  inspects encrypted traffic substitutes its own, which pixi refuses and a
  browser accepts, and that gap is the single most common reason an install
  fails on a laptop that browses the web perfectly well. Then the same question
  in pixi's own words: pixi is made to fetch one small file itself. Those
  connections above are opened by Python, which trusts a different list on every
  operating system — a conda environment that has been moved loses its list
  entirely and then makes every host on the internet look tampered with — and
  pixi getting through is what tells that apart from a machine where something
  really is in the way. Also proxy and certificate variables set in the
  terminal, and a clock wrong enough to make valid certificates look expired.
- **VS Code** — installed, with the Python and Jupyter extensions.

Every problem is printed twice: once as a line in the scan, and once at the
bottom with the command or click-path that fixes it, written out in full,
because a student reading it is by definition having trouble reaching the
website. What was looked at and found fine is counted rather than listed, so
that the thing which is wrong is not hidden among forty ticks; `--verbose`
lists it, and the file written by `--report` holds it either way.

```bash
im doctor                # the whole thing
im doctor -v             # also show what was looked at and was fine
im doctor --offline      # skip the network checks
im doctor --report       # also write im-doctor-report.txt to send to an instructor
im doctor --no-upgrade   # do not offer to upgrade `im` itself first
```

Warnings do not set the exit code; only failures do.

## Keeping itself current

A fix only reaches a hundred students if it arrives, and no student thinks of
upgrading a tool that has never asked them to. So `im` asks on their behalf: at
most once a day, on a background thread so no command ever waits for it, with
the answer cached in the home folder rather than the course folder, which gets
moved and copied and started over.

Which index it asks depends on how this copy was installed, read off the machine
rather than guessed — the conda record in the prefix, the shape of the path
around it, or the absence of both. That same answer decides what is offered:

| how it was installed | what upgrades it |
| --- | --- |
| conda package in a course environment | `pixi update im-course-tools` in the course folder |
| `pixi global install` | `pixi global update im-course-tools` |
| some other conda environment | `conda update -c <its own channel> -c conda-forge im-course-tools` |
| pip | `<that interpreter> -m pip install --upgrade im-course-tools` |
| pipx | `pipx upgrade im-course-tools` |
| a checkout | nothing — the code being run is not the code installed |

Every command prints one line when there is something newer, after its own
output rather than before it. `im update` upgrades `im` first, since it is the
command for putting the environment right and `im` is part of the environment.
`im doctor` asks before doing it, because it otherwise changes nothing.

Neither of them re-runs the command afterwards. `im` cannot replace the files it
is running out of while it is running out of them — on Windows it plainly
cannot — so what follows an upgrade is the command typed back out, to be run
again. An upgrade that finishes cleanly is also checked to have actually changed
the version, read fresh off disk: one that runs, succeeds and changes nothing
would otherwise send a student round the same loop indefinitely.

Set `IM_NO_UPDATE_CHECK=1` to switch the whole thing off.

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
URL, so they never touch the real site, and a suite-wide fixture sets
`IM_NO_UPDATE_CHECK` so no test asks a package index anything. `IM_COURSE_URL`
and `IM_COURSE_FOLDER` are the two overrides they use, and they work by hand
too:

```bash
IM_COURSE_URL=file:///path/to/_book im get iteration
```

## Release

```bash
pixi run release         # bump, tag, and push; CI builds the conda and pip packages
```

## License

MIT
