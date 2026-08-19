# im-course-tools

The `im` command for the [Instructing Machines](https://munch-group.org/instructing-machines)
course: a small, pure-Python CLI that students run from their course folder.

```bash
im check                 # is my environment working?
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
