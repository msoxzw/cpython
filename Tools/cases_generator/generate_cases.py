"""Generate the main interpreter switch.

Reads the instruction definitions from bytecodes.c.
Writes the cases to generated_cases.c.h, which is #included in ceval.c.
"""

import argparse
import contextlib
import dataclasses
import os
import re
import sys
import typing

import parser
from parser import StackEffect

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "../..")
THIS = os.path.relpath(__file__, ROOT)

DEFAULT_INPUT = os.path.relpath(os.path.join(ROOT, "Python/bytecodes.c"))
DEFAULT_OUTPUT = os.path.relpath(os.path.join(ROOT, "Python/generated_cases.c.h"))
DEFAULT_METADATA_OUTPUT = os.path.relpath(
    os.path.join(ROOT, "Python/opcode_metadata.h")
)
BEGIN_MARKER = "// BEGIN BYTECODES //"
END_MARKER = "// END BYTECODES //"
RE_PREDICTED = (
    r"^\s*(?:PREDICT\(|GO_TO_INSTRUCTION\(|DEOPT_IF\(.*?,\s*)(\w+)\);\s*(?://.*)?$"
)
UNUSED = "unused"
BITS_PER_CODE_UNIT = 16

arg_parser = argparse.ArgumentParser(
    description="Generate the code for the interpreter switch.",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
arg_parser.add_argument(
    "-i", "--input", type=str, help="Instruction definitions", default=DEFAULT_INPUT
)
arg_parser.add_argument(
    "-o", "--output", type=str, help="Generated code", default=DEFAULT_OUTPUT
)
arg_parser.add_argument(
    "-m",
    "--metadata",
    action="store_true",
    help=f"Generate metadata instead, changes output default to {DEFAULT_METADATA_OUTPUT}",
)


def effect_size(effect: StackEffect) -> tuple[int, str]:
    """Return the 'size' impact of a stack effect.

    Returns a tuple (numeric, symbolic) where:

    - numeric is an int giving the statically analyzable size of the effect
    - symbolic is a string representing a variable effect (e.g. 'oparg*2')

    At most one of these will be non-zero / non-empty.
    """
    if effect.size:
        assert not effect.cond, "Array effects cannot have a condition"
        return 0, effect.size
    elif effect.cond:
        return 0, f"{maybe_parenthesize(effect.cond)} ? 1 : 0"
    else:
        return 1, ""


def maybe_parenthesize(sym: str) -> str:
    """Add parentheses around a string if it contains an operator.

    An exception is made for '*' which is common and harmless
    in the context where the symbolic size is used.
    """
    if re.match(r"^[\s\w*]+$", sym):
        return sym
    else:
        return f"({sym})"


def list_effect_size(effects: list[StackEffect]) -> tuple[int, str]:
    numeric = 0
    symbolic: list[str] = []
    for effect in effects:
        diff, sym = effect_size(effect)
        numeric += diff
        if sym:
            symbolic.append(maybe_parenthesize(sym))
    return numeric, " + ".join(symbolic)


def string_effect_size(arg: tuple[int, str]) -> str:
    numeric, symbolic = arg
    if numeric and symbolic:
        return f"{numeric} + {symbolic}"
    elif symbolic:
        return symbolic
    else:
        return str(numeric)


class Formatter:
    """Wraps an output stream with the ability to indent etc."""

    stream: typing.TextIO
    prefix: str

    def __init__(self, stream: typing.TextIO, indent: int) -> None:
        self.stream = stream
        self.prefix = " " * indent

    def write_raw(self, s: str) -> None:
        self.stream.write(s)

    def emit(self, arg: str) -> None:
        if arg:
            self.write_raw(f"{self.prefix}{arg}\n")
        else:
            self.write_raw("\n")

    @contextlib.contextmanager
    def indent(self):
        self.prefix += "    "
        yield
        self.prefix = self.prefix[:-4]

    @contextlib.contextmanager
    def block(self, head: str):
        if head:
            self.emit(head + " {")
        else:
            self.emit("{")
        with self.indent():
            yield
        self.emit("}")

    def stack_adjust(
        self,
        diff: int,
        input_effects: list[StackEffect],
        output_effects: list[StackEffect],
    ):
        # TODO: Get rid of 'diff' parameter
        shrink, isym = list_effect_size(input_effects)
        grow, osym = list_effect_size(output_effects)
        diff += grow - shrink
        if isym and isym != osym:
            self.emit(f"STACK_SHRINK({isym});")
        if diff < 0:
            self.emit(f"STACK_SHRINK({-diff});")
        if diff > 0:
            self.emit(f"STACK_GROW({diff});")
        if osym and osym != isym:
            self.emit(f"STACK_GROW({osym});")

    def declare(self, dst: StackEffect, src: StackEffect | None):
        if dst.name == UNUSED:
            return
        typ = f"{dst.type}" if dst.type else "PyObject *"
        if src:
            cast = self.cast(dst, src)
            init = f" = {cast}{src.name}"
        elif dst.cond:
            init = " = NULL"
        else:
            init = ""
        sepa = "" if typ.endswith("*") else " "
        self.emit(f"{typ}{sepa}{dst.name}{init};")

    def assign(self, dst: StackEffect, src: StackEffect):
        if src.name == UNUSED:
            return
        cast = self.cast(dst, src)
        if m := re.match(r"^PEEK\((.*)\)$", dst.name):
            stmt = f"POKE({m.group(1)}, {cast}{src.name});"
            if src.cond:
                stmt = f"if ({src.cond}) {{ {stmt} }}"
            self.emit(stmt)
        elif m := re.match(r"^&PEEK\(.*\)$", dst.name):
            # The user code is responsible for writing to the output array.
            pass
        elif m := re.match(r"^REG\(oparg(\d+)\)$", dst.name):
            self.emit(f"Py_XSETREF({dst.name}, {cast}{src.name});")
        else:
            self.emit(f"{dst.name} = {cast}{src.name};")

    def cast(self, dst: StackEffect, src: StackEffect) -> str:
        return f"({dst.type or 'PyObject *'})" if src.type != dst.type else ""


@dataclasses.dataclass
class Instruction:
    """An instruction with additional data and code."""

    # Parts of the underlying instruction definition
    inst: parser.InstDef
    register: bool
    kind: typing.Literal["inst", "op", "legacy"]  # Legacy means no (input -- output)
    name: str
    block: parser.Block
    block_text: list[str]  # Block.text, less curlies, less PREDICT() calls
    predictions: list[str]  # Prediction targets (instruction names)

    # Computed by constructor
    always_exits: bool
    cache_offset: int
    cache_effects: list[parser.CacheEffect]
    input_effects: list[StackEffect]
    output_effects: list[StackEffect]
    unmoved_names: frozenset[str]
    instr_fmt: str

    # Parallel to input_effects; set later
    input_registers: list[str] = dataclasses.field(repr=False)
    output_registers: list[str] = dataclasses.field(repr=False)

    # Set later
    family: parser.Family | None = None
    predicted: bool = False

    def __init__(self, inst: parser.InstDef):
        self.inst = inst
        self.register = inst.register
        self.kind = inst.kind
        self.name = inst.name
        self.block = inst.block
        self.block_text, self.predictions = extract_block_text(self.block)
        self.always_exits = always_exits(self.block_text)
        self.cache_effects = [
            effect for effect in inst.inputs if isinstance(effect, parser.CacheEffect)
        ]
        self.cache_offset = sum(c.size for c in self.cache_effects)
        self.input_effects = [
            effect for effect in inst.inputs if isinstance(effect, StackEffect)
        ]
        self.output_effects = inst.outputs  # For consistency/completeness
        unmoved_names: set[str] = set()
        for ieffect, oeffect in zip(self.input_effects, self.output_effects):
            if ieffect.name == oeffect.name:
                unmoved_names.add(ieffect.name)
            else:
                break
        self.unmoved_names = frozenset(unmoved_names)
        if self.register:
            num_regs = len(self.input_effects) + len(self.output_effects)
            num_dummies = (num_regs // 2) * 2 + 1 - num_regs
            fmt = "I" + "B" * num_regs + "X" * num_dummies
        else:
            if variable_used(inst, "oparg"):
                fmt = "IB"
            else:
                fmt = "IX"
        cache = "C"
        for ce in self.cache_effects:
            for _ in range(ce.size):
                fmt += cache
                cache = "0"
        self.instr_fmt = fmt

    def analyze_registers(self, a: "Analyzer") -> None:
        regs = iter(("REG(oparg1)", "REG(oparg2)", "REG(oparg3)"))
        try:
            self.input_registers = [
                next(regs) for ieff in self.input_effects if ieff.name != UNUSED
            ]
            self.output_registers = [
                next(regs) for oeff in self.output_effects if oeff.name != UNUSED
            ]
        except StopIteration:  # Running out of registers
            a.error(
                f"Instruction {self.name} has too many register effects", node=self.inst
            )

    def write(self, out: Formatter) -> None:
        """Write one instruction, sans prologue and epilogue."""
        # Write a static assertion that a family's cache size is correct
        if family := self.family:
            if self.name == family.members[0]:
                if cache_size := family.size:
                    out.emit(
                        f"static_assert({cache_size} == "
                        f'{self.cache_offset}, "incorrect cache size");'
                    )

        if not self.register:
            # Write input stack effect variable declarations and initializations
            ieffects = list(reversed(self.input_effects))
            for i, ieffect in enumerate(ieffects):
                isize = string_effect_size(
                    list_effect_size([ieff for ieff in ieffects[: i + 1]])
                )
                if ieffect.size:
                    src = StackEffect(f"&PEEK({isize})", "PyObject **")
                elif ieffect.cond:
                    src = StackEffect(f"({ieffect.cond}) ? PEEK({isize}) : NULL", "")
                else:
                    src = StackEffect(f"PEEK({isize})", "")
                out.declare(ieffect, src)
        else:
            # Write input register variable declarations and initializations
            for ieffect, reg in zip(self.input_effects, self.input_registers):
                src = StackEffect(reg, "")
                out.declare(ieffect, src)

        # Write output stack effect variable declarations
        isize = string_effect_size(list_effect_size(self.input_effects))
        input_names = {ieffect.name for ieffect in self.input_effects}
        for i, oeffect in enumerate(self.output_effects):
            if oeffect.name not in input_names:
                if oeffect.size:
                    osize = string_effect_size(
                        list_effect_size([oeff for oeff in self.output_effects[:i]])
                    )
                    offset = "stack_pointer"
                    if isize != osize:
                        if isize != "0":
                            offset += f" - ({isize})"
                        if osize != "0":
                            offset += f" + {osize}"
                    src = StackEffect(offset, "PyObject **")
                    out.declare(oeffect, src)
                else:
                    out.declare(oeffect, None)

        # out.emit(f"JUMPBY(OPSIZE({self.inst.name}) - 1);")

        self.write_body(out, 0)

        # Skip the rest if the block always exits
        if self.always_exits:
            return

        if not self.register:
            # Write net stack growth/shrinkage
            out.stack_adjust(
                0,
                [ieff for ieff in self.input_effects],
                [oeff for oeff in self.output_effects],
            )

            # Write output stack effect assignments
            oeffects = list(reversed(self.output_effects))
            for i, oeffect in enumerate(oeffects):
                if oeffect.name in self.unmoved_names:
                    continue
                osize = string_effect_size(
                    list_effect_size([oeff for oeff in oeffects[: i + 1]])
                )
                if oeffect.size:
                    dst = StackEffect(f"&PEEK({osize})", "PyObject **")
                else:
                    dst = StackEffect(f"PEEK({osize})", "")
                out.assign(dst, oeffect)
        else:
            # Write output register assignments
            for oeffect, reg in zip(self.output_effects, self.output_registers):
                dst = StackEffect(reg, "")
                out.assign(dst, oeffect)

        # Write cache effect
        if self.cache_offset:
            out.emit(f"JUMPBY({self.cache_offset});")

    def write_body(self, out: Formatter, dedent: int, cache_adjust: int = 0) -> None:
        """Write the instruction body."""
        # Write cache effect variable declarations and initializations
        cache_offset = cache_adjust
        for ceffect in self.cache_effects:
            if ceffect.name != UNUSED:
                bits = ceffect.size * BITS_PER_CODE_UNIT
                if bits == 64:
                    # NOTE: We assume that 64-bit data in the cache
                    # is always an object pointer.
                    # If this becomes false, we need a way to specify
                    # syntactically what type the cache data is.
                    typ = "PyObject *"
                    func = "read_obj"
                else:
                    typ = f"uint{bits}_t "
                    func = f"read_u{bits}"
                out.emit(
                    f"{typ}{ceffect.name} = {func}(&next_instr[{cache_offset}].cache);"
                )
            cache_offset += ceffect.size
        assert cache_offset == self.cache_offset + cache_adjust

        # Write the body, substituting a goto for ERROR_IF() and other stuff
        assert dedent <= 0
        extra = " " * -dedent
        for line in self.block_text:
            if m := re.match(r"(\s*)ERROR_IF\((.+), (\w+)\);\s*(?://.*)?$", line):
                space, cond, label = m.groups()
                # ERROR_IF() must pop the inputs from the stack.
                # The code block is responsible for DECREF()ing them.
                # NOTE: If the label doesn't exist, just add it to ceval.c.
                if not self.register:
                    # Don't pop common input/output effects at the bottom!
                    # These aren't DECREF'ed so they can stay.
                    ieffs = list(self.input_effects)
                    oeffs = list(self.output_effects)
                    while ieffs and oeffs and ieffs[0] == oeffs[0]:
                        ieffs.pop(0)
                        oeffs.pop(0)
                    ninputs, symbolic = list_effect_size(ieffs)
                    if ninputs:
                        label = f"pop_{ninputs}_{label}"
                else:
                    symbolic = ""
                if symbolic:
                    out.write_raw(
                        f"{extra}{space}if ({cond}) {{ STACK_SHRINK({symbolic}); goto {label}; }}\n"
                    )
                else:
                    out.write_raw(f"{extra}{space}if ({cond}) goto {label};\n")
            elif m := re.match(r"(\s*)DECREF_INPUTS\(\);\s*(?://.*)?$", line):
                if not self.register:
                    space = m.group(1)
                    for ieff in self.input_effects:
                        if ieff.name not in self.unmoved_names:
                            out.write_raw(f"{extra}{space}Py_DECREF({ieff.name});\n")
            else:
                out.write_raw(extra + line)


InstructionOrCacheEffect = Instruction | parser.CacheEffect
StackEffectMapping = list[tuple[StackEffect, StackEffect]]


@dataclasses.dataclass
class Component:
    instr: Instruction
    input_mapping: StackEffectMapping
    output_mapping: StackEffectMapping

    def write_body(self, out: Formatter, cache_adjust: int) -> None:
        with out.block(""):
            input_names = {ieffect.name for _, ieffect in self.input_mapping}
            for var, ieffect in self.input_mapping:
                out.declare(ieffect, var)
            for _, oeffect in self.output_mapping:
                if oeffect.name not in input_names:
                    out.declare(oeffect, None)

            self.instr.write_body(out, dedent=-4, cache_adjust=cache_adjust)

            for var, oeffect in self.output_mapping:
                out.assign(var, oeffect)


@dataclasses.dataclass
class SuperOrMacroInstruction:
    """Common fields for super- and macro instructions."""

    name: str
    stack: list[StackEffect]
    initial_sp: int
    final_sp: int
    instr_fmt: str


@dataclasses.dataclass
class SuperInstruction(SuperOrMacroInstruction):
    """A super-instruction."""

    super: parser.Super
    parts: list[Component]


@dataclasses.dataclass
class MacroInstruction(SuperOrMacroInstruction):
    """A macro instruction."""

    macro: parser.Macro
    parts: list[Component | parser.CacheEffect]


AnyInstruction = Instruction | SuperInstruction | MacroInstruction
INSTR_FMT_PREFIX = "INSTR_FMT_"


class Analyzer:
    """Parse input, analyze it, and write to output."""

    filename: str
    output_filename: str
    src: str
    errors: int = 0

    def __init__(self, filename: str, output_filename: str):
        """Read the input file."""
        self.filename = filename
        self.output_filename = output_filename
        with open(filename) as f:
            self.src = f.read()

    def error(self, msg: str, node: parser.Node) -> None:
        lineno = 0
        if context := node.context:
            # Use line number of first non-comment in the node
            for token in context.owner.tokens[context.begin : context.end]:
                lineno = token.line
                if token.kind != "COMMENT":
                    break
        print(f"{self.filename}:{lineno}: {msg}", file=sys.stderr)
        self.errors += 1

    everything: list[parser.InstDef | parser.Super | parser.Macro]
    instrs: dict[str, Instruction]  # Includes ops
    supers: dict[str, parser.Super]
    super_instrs: dict[str, SuperInstruction]
    macros: dict[str, parser.Macro]
    macro_instrs: dict[str, MacroInstruction]
    families: dict[str, parser.Family]

    def parse(self) -> None:
        """Parse the source text.

        We only want the parser to see the stuff between the
        begin and end markers.
        """
        psr = parser.Parser(self.src, filename=self.filename)

        # Skip until begin marker
        while tkn := psr.next(raw=True):
            if tkn.text == BEGIN_MARKER:
                break
        else:
            raise psr.make_syntax_error(
                f"Couldn't find {BEGIN_MARKER!r} in {psr.filename}"
            )
        start = psr.getpos()

        # Find end marker, then delete everything after it
        while tkn := psr.next(raw=True):
            if tkn.text == END_MARKER:
                break
        del psr.tokens[psr.getpos() - 1 :]

        # Parse from start
        psr.setpos(start)
        self.everything = []
        self.instrs = {}
        self.supers = {}
        self.macros = {}
        self.families = {}
        thing: parser.InstDef | parser.Super | parser.Macro | parser.Family | None
        while thing := psr.definition():
            match thing:
                case parser.InstDef(name=name):
                    self.instrs[name] = Instruction(thing)
                    self.everything.append(thing)
                case parser.Super(name):
                    self.supers[name] = thing
                    self.everything.append(thing)
                case parser.Macro(name):
                    self.macros[name] = thing
                    self.everything.append(thing)
                case parser.Family(name):
                    self.families[name] = thing
                case _:
                    typing.assert_never(thing)
        if not psr.eof():
            raise psr.make_syntax_error("Extra stuff at the end")

        print(
            f"Read {len(self.instrs)} instructions/ops, "
            f"{len(self.supers)} supers, {len(self.macros)} macros, "
            f"and {len(self.families)} families from {self.filename}",
            file=sys.stderr,
        )

    def analyze(self) -> None:
        """Analyze the inputs.

        Raises SystemExit if there is an error.
        """
        self.find_predictions()
        self.analyze_register_instrs()
        self.analyze_supers_and_macros()
        self.map_families()
        self.check_families()

    def find_predictions(self) -> None:
        """Find the instructions that need PREDICTED() labels."""
        for instr in self.instrs.values():
            targets = set(instr.predictions)
            for line in instr.block_text:
                if m := re.match(RE_PREDICTED, line):
                    targets.add(m.group(1))
            for target in targets:
                if target_instr := self.instrs.get(target):
                    target_instr.predicted = True
                else:
                    self.error(
                        f"Unknown instruction {target!r} predicted in {instr.name!r}",
                        instr.inst,  # TODO: Use better location
                    )

    def map_families(self) -> None:
        """Link instruction names back to their family, if they have one."""
        for family in self.families.values():
            for member in family.members:
                if member_instr := self.instrs.get(member):
                    if member_instr.family not in (family, None):
                        self.error(
                            f"Instruction {member} is a member of multiple families "
                            f"({member_instr.family.name}, {family.name}).",
                            family,
                        )
                    else:
                        member_instr.family = family
                elif member_macro := self.macro_instrs.get(member):
                    for part in member_macro.parts:
                        if isinstance(part, Component):
                            if part.instr.family not in (family, None):
                                self.error(
                                    f"Component {part.instr.name} of macro {member} "
                                    f"is a member of multiple families "
                                    f"({part.instr.family.name}, {family.name}).",
                                    family,
                                )
                            else:
                                part.instr.family = family
                else:
                    self.error(
                        f"Unknown instruction {member!r} referenced in family {family.name!r}",
                        family,
                    )

    def check_families(self) -> None:
        """Check each family:

        - Must have at least 2 members
        - All members must be known instructions
        - All members must have the same cache, input and output effects
        """
        for family in self.families.values():
            if len(family.members) < 2:
                self.error(f"Family {family.name!r} has insufficient members", family)
            members = [
                member
                for member in family.members
                if member in self.instrs or member in self.macro_instrs
            ]
            if members != family.members:
                unknown = set(family.members) - set(members)
                self.error(
                    f"Family {family.name!r} has unknown members: {unknown}", family
                )
            if len(members) < 2:
                continue
            expected_effects = self.effect_counts(members[0])
            for member in members[1:]:
                member_effects = self.effect_counts(member)
                if member_effects != expected_effects:
                    self.error(
                        f"Family {family.name!r} has inconsistent "
                        f"(cache, input, output) effects:\n"
                        f"  {family.members[0]} = {expected_effects}; "
                        f"{member} = {member_effects}",
                        family,
                    )

    def effect_counts(self, name: str) -> tuple[int, int, int]:
        if instr := self.instrs.get(name):
            cache = instr.cache_offset
            input = len(instr.input_effects)
            output = len(instr.output_effects)
        elif macro := self.macro_instrs.get(name):
            cache, input, output = 0, 0, 0
            for part in macro.parts:
                if isinstance(part, Component):
                    cache += part.instr.cache_offset
                    # A component may pop what the previous component pushed,
                    # so we offset the input/output counts by that.
                    delta_i = len(part.instr.input_effects)
                    delta_o = len(part.instr.output_effects)
                    offset = min(delta_i, output)
                    input += delta_i - offset
                    output += delta_o - offset
                else:
                    assert isinstance(part, parser.CacheEffect), part
                    cache += part.size
        else:
            assert False, f"Unknown instruction {name!r}"
        return cache, input, output

    def analyze_register_instrs(self) -> None:
        for instr in self.instrs.values():
            if instr.register:
                instr.analyze_registers(self)

    def analyze_supers_and_macros(self) -> None:
        """Analyze each super- and macro instruction."""
        self.super_instrs = {}
        self.macro_instrs = {}
        for name, super in self.supers.items():
            self.super_instrs[name] = self.analyze_super(super)
        for name, macro in self.macros.items():
            self.macro_instrs[name] = self.analyze_macro(macro)

    def analyze_super(self, super: parser.Super) -> SuperInstruction:
        components = self.check_super_components(super)
        stack, initial_sp = self.stack_analysis(components)
        sp = initial_sp
        parts: list[Component] = []
        format = ""
        for instr in components:
            part, sp = self.analyze_instruction(instr, stack, sp)
            parts.append(part)
            format += instr.instr_fmt
        final_sp = sp
        return SuperInstruction(
            super.name, stack, initial_sp, final_sp, format, super, parts
        )

    def analyze_macro(self, macro: parser.Macro) -> MacroInstruction:
        components = self.check_macro_components(macro)
        stack, initial_sp = self.stack_analysis(components)
        sp = initial_sp
        parts: list[Component | parser.CacheEffect] = []
        format = "IB"  # Macros don't support register instructions yet
        cache = "C"
        for component in components:
            match component:
                case parser.CacheEffect() as ceffect:
                    parts.append(ceffect)
                    for _ in range(ceffect.size):
                        format += cache
                        cache = "0"
                case Instruction() as instr:
                    part, sp = self.analyze_instruction(instr, stack, sp)
                    parts.append(part)
                    for ce in instr.cache_effects:
                        for _ in range(ce.size):
                            format += cache
                            cache = "0"
                case _:
                    typing.assert_never(component)
        final_sp = sp
        return MacroInstruction(
            macro.name, stack, initial_sp, final_sp, format, macro, parts
        )

    def analyze_instruction(
        self, instr: Instruction, stack: list[StackEffect], sp: int
    ) -> tuple[Component, int]:
        input_mapping: StackEffectMapping = []
        for ieffect in reversed(instr.input_effects):
            sp -= 1
            input_mapping.append((stack[sp], ieffect))
        output_mapping: StackEffectMapping = []
        for oeffect in instr.output_effects:
            output_mapping.append((stack[sp], oeffect))
            sp += 1
        return Component(instr, input_mapping, output_mapping), sp

    def check_super_components(self, super: parser.Super) -> list[Instruction]:
        components: list[Instruction] = []
        for op in super.ops:
            if op.name not in self.instrs:
                self.error(f"Unknown instruction {op.name!r}", super)
            else:
                components.append(self.instrs[op.name])
        return components

    def check_macro_components(
        self, macro: parser.Macro
    ) -> list[InstructionOrCacheEffect]:
        components: list[InstructionOrCacheEffect] = []
        for uop in macro.uops:
            match uop:
                case parser.OpName(name):
                    if name not in self.instrs:
                        self.error(f"Unknown instruction {name!r}", macro)
                    components.append(self.instrs[name])
                case parser.CacheEffect():
                    components.append(uop)
                case _:
                    typing.assert_never(uop)
        return components

    def stack_analysis(
        self, components: typing.Iterable[InstructionOrCacheEffect]
    ) -> tuple[list[StackEffect], int]:
        """Analyze a super-instruction or macro.

        Ignore cache effects.

        Return the list of variable names and the initial stack pointer.
        """
        lowest = current = highest = 0
        for thing in components:
            match thing:
                case Instruction() as instr:
                    if any(
                        eff.size for eff in instr.input_effects + instr.output_effects
                    ):
                        # TODO: Eventually this will be needed, at least for macros.
                        self.error(
                            f"Instruction {instr.name!r} has variable-sized stack effect, "
                            "which are not supported in super- or macro instructions",
                            instr.inst,  # TODO: Pass name+location of super/macro
                        )
                    current -= len(instr.input_effects)
                    lowest = min(lowest, current)
                    current += len(instr.output_effects)
                    highest = max(highest, current)
                case parser.CacheEffect():
                    pass
                case _:
                    typing.assert_never(thing)
        # At this point, 'current' is the net stack effect,
        # and 'lowest' and 'highest' are the extremes.
        # Note that 'lowest' may be negative.
        # TODO: Reverse the numbering.
        stack = [
            StackEffect(f"_tmp_{i+1}", "") for i in reversed(range(highest - lowest))
        ]
        return stack, -lowest

    def get_stack_effect_info(
        self, thing: parser.InstDef | parser.Super | parser.Macro
    ) -> tuple[AnyInstruction | None, str, str]:
        def effect_str(effects: list[StackEffect]) -> str:
            if getattr(thing, "kind", None) == "legacy":
                return str(-1)
            n_effect, sym_effect = list_effect_size(effects)
            if sym_effect:
                return f"{sym_effect} + {n_effect}" if n_effect else sym_effect
            return str(n_effect)

        instr: AnyInstruction | None
        match thing:
            case parser.InstDef():
                if thing.kind != "op":
                    instr = self.instrs[thing.name]
                    popped = effect_str(instr.input_effects)
                    pushed = effect_str(instr.output_effects)
                else:
                    instr = None
                    popped = ""
                    pushed = ""
            case parser.Super():
                instr = self.super_instrs[thing.name]
                popped = "+".join(
                    effect_str(comp.instr.input_effects) for comp in instr.parts
                )
                pushed = "+".join(
                    effect_str(comp.instr.output_effects) for comp in instr.parts
                )
            case parser.Macro():
                instr = self.macro_instrs[thing.name]
                parts = [comp for comp in instr.parts if isinstance(comp, Component)]
                popped = "+".join(
                    effect_str(comp.instr.input_effects) for comp in parts
                )
                pushed = "+".join(
                    effect_str(comp.instr.output_effects) for comp in parts
                )
            case _:
                typing.assert_never(thing)
        return instr, popped, pushed

    def write_stack_effect_functions(self) -> None:
        popped_data: list[tuple[AnyInstruction, str]] = []
        pushed_data: list[tuple[AnyInstruction, str]] = []
        for thing in self.everything:
            instr, popped, pushed = self.get_stack_effect_info(thing)
            if instr is not None:
                popped_data.append((instr, popped))
                pushed_data.append((instr, pushed))

        def write_function(
            direction: str, data: list[tuple[AnyInstruction, str]]
        ) -> None:
            self.out.emit("\n#ifndef NDEBUG")
            self.out.emit("static int")
            self.out.emit(f"_PyOpcode_num_{direction}(int opcode, int oparg, bool jump) {{")
            self.out.emit("    switch(opcode) {")
            for instr, effect in data:
                self.out.emit(f"        case {instr.name}:")
                self.out.emit(f"            return {effect};")
            self.out.emit("        default:")
            self.out.emit("            Py_UNREACHABLE();")
            self.out.emit("    }")
            self.out.emit("}")
            self.out.emit("#endif")

        write_function("popped", popped_data)
        write_function("pushed", pushed_data)

    def write_metadata(self) -> None:
        """Write instruction metadata to output file."""

        # Compute the set of all instruction formats.
        all_formats: set[str] = set()
        for thing in self.everything:
            match thing:
                case parser.InstDef():
                    format = self.instrs[thing.name].instr_fmt
                case parser.Super():
                    format = self.super_instrs[thing.name].instr_fmt
                case parser.Macro():
                    format = self.macro_instrs[thing.name].instr_fmt
                case _:
                    typing.assert_never(thing)
            all_formats.add(format)
        # Turn it into a list of enum definitions.
        format_enums = [INSTR_FMT_PREFIX + format for format in sorted(all_formats)]

        with open(self.output_filename, "w") as f:
            # Write provenance header
            f.write(f"// This file is generated by {THIS} --metadata\n")
            f.write(f"// from {os.path.relpath(self.filename, ROOT)}\n")
            f.write(f"// Do not edit!\n")

            # Create formatter; the rest of the code uses this
            self.out = Formatter(f, 0)

            self.write_stack_effect_functions()

            # Write variable definition
            self.out.emit("enum Direction { DIR_NONE, DIR_READ, DIR_WRITE };")
            self.out.emit(f"enum InstructionFormat {{ {', '.join(format_enums)} }};")
            self.out.emit("struct opcode_metadata {")
            with self.out.indent():
                self.out.emit("enum Direction dir_op1;")
                self.out.emit("enum Direction dir_op2;")
                self.out.emit("enum Direction dir_op3;")
                self.out.emit("bool valid_entry;")
                self.out.emit("enum InstructionFormat instr_format;")
            self.out.emit("} _PyOpcode_opcode_metadata[256] = {")

            # Write metadata for each instruction
            for thing in self.everything:
                match thing:
                    case parser.InstDef():
                        if thing.kind != "op":
                            self.write_metadata_for_inst(self.instrs[thing.name])
                    case parser.Super():
                        self.write_metadata_for_super(self.super_instrs[thing.name])
                    case parser.Macro():
                        self.write_metadata_for_macro(self.macro_instrs[thing.name])
                    case _:
                        typing.assert_never(thing)

            # Write end of array
            self.out.emit("};")

    def write_metadata_for_inst(self, instr: Instruction) -> None:
        """Write metadata for a single instruction."""
        dir_op1 = dir_op2 = dir_op3 = "DIR_NONE"
        if instr.kind == "legacy":
            assert not instr.register
        else:
            if instr.register:
                directions: list[str] = []
                directions.extend("DIR_READ" for _ in instr.input_effects)
                directions.extend("DIR_WRITE" for _ in instr.output_effects)
                directions.extend("DIR_NONE" for _ in range(3))
                dir_op1, dir_op2, dir_op3 = directions[:3]
        self.out.emit(
            f"    [{instr.name}] = {{ {dir_op1}, {dir_op2}, {dir_op3}, true, {INSTR_FMT_PREFIX}{instr.instr_fmt} }},"
        )

    def write_metadata_for_super(self, sup: SuperInstruction) -> None:
        """Write metadata for a super-instruction."""
        dir_op1 = dir_op2 = dir_op3 = "DIR_NONE"
        self.out.emit(
            f"    [{sup.name}] = {{ {dir_op1}, {dir_op2}, {dir_op3}, true, {INSTR_FMT_PREFIX}{sup.instr_fmt} }},"
        )

    def write_metadata_for_macro(self, mac: MacroInstruction) -> None:
        """Write metadata for a macro-instruction."""
        dir_op1 = dir_op2 = dir_op3 = "DIR_NONE"
        self.out.emit(
            f"    [{mac.name}] = {{ {dir_op1}, {dir_op2}, {dir_op3}, true, {INSTR_FMT_PREFIX}{mac.instr_fmt} }},"
        )

    def write_instructions(self) -> None:
        """Write instructions to output file."""
        with open(self.output_filename, "w") as f:
            # Write provenance header
            f.write(f"// This file is generated by {THIS}\n")
            f.write(f"// from {os.path.relpath(self.filename, ROOT)}\n")
            f.write(f"// Do not edit!\n")

            # Create formatter; the rest of the code uses this
            self.out = Formatter(f, 8)

            # Write and count instructions of all kinds
            n_instrs = 0
            n_supers = 0
            n_macros = 0
            for thing in self.everything:
                match thing:
                    case parser.InstDef():
                        if thing.kind != "op":
                            n_instrs += 1
                            self.write_instr(self.instrs[thing.name])
                    case parser.Super():
                        n_supers += 1
                        self.write_super(self.super_instrs[thing.name])
                    case parser.Macro():
                        n_macros += 1
                        self.write_macro(self.macro_instrs[thing.name])
                    case _:
                        typing.assert_never(thing)

        print(
            f"Wrote {n_instrs} instructions, {n_supers} supers, "
            f"and {n_macros} macros to {self.output_filename}",
            file=sys.stderr,
        )

    def write_instr(self, instr: Instruction) -> None:
        name = instr.name
        self.out.emit("")
        with self.out.block(f"TARGET({name})"):
            if instr.predicted:
                self.out.emit(f"PREDICTED({name});")
            instr.write(self.out)
            if not instr.always_exits:
                for prediction in instr.predictions:
                    self.out.emit(f"PREDICT({prediction});")
                self.out.emit(f"DISPATCH();")

    def write_super(self, sup: SuperInstruction) -> None:
        """Write code for a super-instruction."""
        with self.wrap_super_or_macro(sup):
            first = True
            for comp in sup.parts:
                if first:
                    pass
                    # self.out.emit("JUMPBY(OPSIZE(opcode) - 1);")
                else:
                    self.out.emit("NEXTOPARG();")
                    self.out.emit("JUMPBY(1);")
                    # self.out.emit("JUMPBY(OPSIZE(opcode));")
                first = False
                comp.write_body(self.out, 0)
                if comp.instr.cache_offset:
                    self.out.emit(f"JUMPBY({comp.instr.cache_offset});")

    def write_macro(self, mac: MacroInstruction) -> None:
        """Write code for a macro instruction."""
        last_instr: Instruction | None = None
        with self.wrap_super_or_macro(mac):
            cache_adjust = 0
            for part in mac.parts:
                match part:
                    case parser.CacheEffect(size=size):
                        cache_adjust += size
                    case Component() as comp:
                        last_instr = comp.instr
                        comp.write_body(self.out, cache_adjust)
                        cache_adjust += comp.instr.cache_offset

            if cache_adjust:
                self.out.emit(f"JUMPBY({cache_adjust});")

            if (
                last_instr
                and (family := last_instr.family)
                and mac.name == family.members[0]
                and (cache_size := family.size)
            ):
                self.out.emit(
                    f"static_assert({cache_size} == "
                    f'{cache_adjust}, "incorrect cache size");'
                )

    @contextlib.contextmanager
    def wrap_super_or_macro(self, up: SuperOrMacroInstruction):
        """Shared boilerplate for super- and macro instructions."""
        # TODO: Somewhere (where?) make it so that if one instruction
        # has an output that is input to another, and the variable names
        # and types match and don't conflict with other instructions,
        # that variable is declared with the right name and type in the
        # outer block, rather than trusting the compiler to optimize it.
        self.out.emit("")
        with self.out.block(f"TARGET({up.name})"):
            for i, var in reversed(list(enumerate(up.stack))):
                src = None
                if i < up.initial_sp:
                    src = StackEffect(f"PEEK({up.initial_sp - i})", "")
                self.out.declare(var, src)

            yield

            # TODO: Use slices of up.stack instead of numeric values
            self.out.stack_adjust(up.final_sp - up.initial_sp, [], [])

            for i, var in enumerate(reversed(up.stack[: up.final_sp]), 1):
                dst = StackEffect(f"PEEK({i})", "")
                self.out.assign(dst, var)

            self.out.emit(f"DISPATCH();")


def extract_block_text(block: parser.Block) -> tuple[list[str], list[str]]:
    # Get lines of text with proper dedent
    blocklines = block.text.splitlines(True)

    # Remove blank lines from both ends
    while blocklines and not blocklines[0].strip():
        blocklines.pop(0)
    while blocklines and not blocklines[-1].strip():
        blocklines.pop()

    # Remove leading and trailing braces
    assert blocklines and blocklines[0].strip() == "{"
    assert blocklines and blocklines[-1].strip() == "}"
    blocklines.pop()
    blocklines.pop(0)

    # Remove trailing blank lines
    while blocklines and not blocklines[-1].strip():
        blocklines.pop()

    # Separate PREDICT(...) macros from end
    predictions: list[str] = []
    while blocklines and (
        m := re.match(r"^\s*PREDICT\((\w+)\);\s*(?://.*)?$", blocklines[-1])
    ):
        predictions.insert(0, m.group(1))
        blocklines.pop()

    return blocklines, predictions


def always_exits(lines: list[str]) -> bool:
    """Determine whether a block always ends in a return/goto/etc."""
    if not lines:
        return False
    line = lines[-1].rstrip()
    # Indent must match exactly (TODO: Do something better)
    if line[:12] != " " * 12:
        return False
    line = line[12:]
    return line.startswith(
        (
            "goto ",
            "return ",
            "DISPATCH",
            "GO_TO_",
            "Py_UNREACHABLE()",
            "ERROR_IF(true, ",
        )
    )


def variable_used(node: parser.Node, name: str) -> bool:
    """Determine whether a variable with a given name is used in a node."""
    return any(
        token.kind == "IDENTIFIER" and token.text == name for token in node.tokens
    )


def main():
    """Parse command line, parse input, analyze, write output."""
    args = arg_parser.parse_args()  # Prints message and sys.exit(2) on error
    if args.metadata:
        if args.output == DEFAULT_OUTPUT:
            args.output = DEFAULT_METADATA_OUTPUT
    a = Analyzer(args.input, args.output)  # Raises OSError if input unreadable
    a.parse()  # Raises SyntaxError on failure
    a.analyze()  # Prints messages and sets a.errors on failure
    if a.errors:
        sys.exit(f"Found {a.errors} errors")
    if args.metadata:
        a.write_metadata()
    else:
        a.write_instructions()  # Raises OSError if output can't be written


if __name__ == "__main__":
    main()
