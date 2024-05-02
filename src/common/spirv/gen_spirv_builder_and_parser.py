#!/usr/bin/python3
# Copyright 2021 The ANGLE Project Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# gen_spirv_builder_and_parser.py:
#   Code generation for SPIR-V instruction builder and parser.
#   NOTE: don't run this script directly. Run scripts/run_code_generation.py.

import itertools
import json
import os
import sys

# ANGLE uses SPIR-V 1.0 currently, so there's no reason to generate code for newer instructions.
SPIRV_GRAMMAR_FILE = '../../../third_party/vulkan-deps/spirv-headers/src/include/spirv/1.0/spirv.core.grammar.json'

# Cherry pick some extra extensions from here that aren't in SPIR-V 1.0.
SPIRV_CHERRY_PICKED_EXTENSIONS_FILE = '../../../third_party/vulkan-deps/spirv-headers/src/include/spirv/unified1/spirv.core.grammar.json'

# The script has two sets of outputs, a header and source file for SPIR-V code generation, and a
# header and source file for SPIR-V parsing.
SPIRV_BUILDER_FILE = 'spirv_instruction_builder'
SPIRV_PARSER_FILE = 'spirv_instruction_parser'

# The types are either defined in spirv_types.h (to use strong types), or are enums that are
# defined by SPIR-V headers.
ANGLE_DEFINED_TYPES = [
    'IdRef', 'IdResult', 'IdResultType', 'IdMemorySemantics', 'IdScope', 'LiteralInteger',
    'LiteralString', 'LiteralContextDependentNumber', 'LiteralExtInstInteger',
    'PairLiteralIntegerIdRef', 'PairIdRefLiteralInteger', 'PairIdRefIdRef'
]

HEADER_TEMPLATE = """// GENERATED FILE - DO NOT EDIT.
// Generated by {script_name} using data from {data_source_name}.
//
// Copyright 2021 The ANGLE Project Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.
//
// {file_name}_autogen.h:
//   Functions to {verb} SPIR-V binary for each instruction.

#ifndef COMMON_SPIRV_{file_name_capitalized}AUTOGEN_H_
#define COMMON_SPIRV_{file_name_capitalized}AUTOGEN_H_

#include <spirv/unified1/spirv.hpp>

#include "spirv_types.h"

namespace angle
{{
namespace spirv
{{
{prototype_list}
}}  // namespace spirv
}}  // namespace angle

#endif // COMMON_SPIRV_{file_name_capitalized}AUTOGEN_H_
"""

SOURCE_TEMPLATE = """// GENERATED FILE - DO NOT EDIT.
// Generated by {script_name} using data from {data_source_name}.
//
// Copyright 2021 The ANGLE Project Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.
//
// {file_name}_autogen.cpp:
//   Functions to {verb} SPIR-V binary for each instruction.

#include "{file_name}_autogen.h"

#include <string.h>

#include "common/debug.h"

namespace angle
{{
namespace spirv
{{
{helper_functions}

{function_list}
}}  // namespace spirv
}}  // namespace angle
"""

BUILDER_HELPER_FUNCTIONS = """namespace
{
uint32_t MakeLengthOp(size_t length, spv::Op op)
{
    ASSERT(length <= 0xFFFFu);
    ASSERT(op <= 0xFFFFu);

    // It's easy for a complex shader to be crafted to hit the length limit,
    // turn that into a crash instead of a security bug.  Ideally, the compiler
    // would gracefully fail compilation, so this is more of a safety net.
    if (ANGLE_UNLIKELY(length > 0xFFFFu))
    {
        ERR() << "Complex shader not representible in SPIR-V";
        ANGLE_CRASH();
    }

    return static_cast<uint32_t>(length) << 16 | op;
}
}  // anonymous namespace

void WriteSpirvHeader(std::vector<uint32_t> *blob, uint32_t idCount)
{
    // Header:
    //
    //  - Magic number
    //  - Version (1.0)
    //  - ANGLE's Generator number:
    //     * 24 for tool id (higher 16 bits)
    //     * 1 for tool version (lower 16 bits))
    //  - Bound (idCount)
    //  - 0 (reserved)
    constexpr uint32_t kANGLEGeneratorId = 24;
    constexpr uint32_t kANGLEGeneratorVersion = 1;

    ASSERT(blob->empty());

    blob->push_back(spv::MagicNumber);
    blob->push_back(0x00010000);
    blob->push_back(kANGLEGeneratorId << 16 | kANGLEGeneratorVersion);
    blob->push_back(idCount);
    blob->push_back(0x00000000);
}
"""

BUILDER_HELPER_FUNCTION_PROTOTYPE = """
    void WriteSpirvHeader(std::vector<uint32_t> *blob, uint32_t idCount);
"""

PARSER_FIXED_FUNCTIONS_PROTOTYPES = """void GetInstructionOpAndLength(const uint32_t *_instruction,
    spv::Op *opOut, uint32_t *lengthOut);
"""

PARSER_FIXED_FUNCTIONS = """void GetInstructionOpAndLength(const uint32_t *_instruction,
    spv::Op *opOut, uint32_t *lengthOut)
{
    constexpr uint32_t kOpMask = 0xFFFFu;
    *opOut = static_cast<spv::Op>(_instruction[0] & kOpMask);
    *lengthOut = _instruction[0] >> 16;
}
"""

TEMPLATE_BUILDER_FUNCTION_PROTOTYPE = """void Write{op}(Blob *blob {param_list})"""
TEMPLATE_BUILDER_FUNCTION_BODY = """{{
    const size_t startSize = blob->size();
    blob->push_back(0);
    {push_back_lines}
    (*blob)[startSize] = MakeLengthOp(blob->size() - startSize, spv::Op{op});
}}
"""

TEMPLATE_PARSER_FUNCTION_PROTOTYPE = """void Parse{op}(const uint32_t *_instruction {param_list})"""
TEMPLATE_PARSER_FUNCTION_BODY = """{{
    spv::Op _op;
    uint32_t _length;
    GetInstructionOpAndLength(_instruction, &_op, &_length);
    ASSERT(_op == spv::Op{op});
    uint32_t _o = 1;
    {parse_lines}
}}
"""


def load_grammar(grammar_file):
    with open(grammar_file) as grammar_in:
        grammar = json.loads(grammar_in.read())

    return grammar


def remove_chars(string, chars):
    return ''.join(list(filter(lambda c: c not in chars, string)))


def make_camel_case(name):
    return name[0].lower() + name[1:]


class Writer:

    def __init__(self):
        self.path_prefix = os.path.dirname(os.path.realpath(__file__)) + os.path.sep
        self.grammar = load_grammar(self.path_prefix + SPIRV_GRAMMAR_FILE)

        # We need some extensions that aren't in SPIR-V 1.0. Cherry pick them into our grammar.
        cherry_picked_extensions = {'SPV_EXT_fragment_shader_interlock'}
        cherry_picked_extensions_grammar = load_grammar(self.path_prefix +
                                                        SPIRV_CHERRY_PICKED_EXTENSIONS_FILE)
        self.grammar['instructions'] += [
            i for i in cherry_picked_extensions_grammar['instructions']
            if 'extensions' in i and set(i['extensions']) & cherry_picked_extensions
        ]

        # If an instruction has a parameter of these types, the instruction is ignored
        self.unsupported_kinds = set(['LiteralSpecConstantOpInteger'])
        # If an instruction requires a capability of these kinds, the instruction is ignored
        self.unsupported_capabilities = set(['Kernel', 'Addresses'])
        # If an instruction requires an extension other than these, the instruction is ignored
        self.supported_extensions = set([]) | cherry_picked_extensions
        # List of bit masks.  These have 'Mask' added to their typename in SPIR-V headers.
        self.bit_mask_types = set([])

        # List of generated instructions builder/parser functions so far.
        self.instruction_builder_prototypes = [BUILDER_HELPER_FUNCTION_PROTOTYPE]
        self.instruction_builder_impl = []
        self.instruction_parser_prototypes = [PARSER_FIXED_FUNCTIONS_PROTOTYPES]
        self.instruction_parser_impl = [PARSER_FIXED_FUNCTIONS]

    def write_builder_and_parser(self):
        """Generates four files, a set of header and source files for generating SPIR-V instructions
        and a set for parsing SPIR-V instructions.  Only Vulkan instructions are processed (and not
        OpenCL for example), and some assumptions are made based on ANGLE's usage (for example that
        constants always fit in one 32-bit unit, as GLES doesn't support double or 64-bit types).

        Additionally, enums and other parameter 'kinds' are not parsed from the json file, but
        rather use the definitions from the SPIR-V headers repository and the spirv_types.h file."""

        # Recurse through capabilities and accumulate ones that depend on unsupported ones.
        self.accumulate_unsupported_capabilities()

        self.find_bit_mask_types()

        for instruction in self.grammar['instructions']:
            self.generate_instruction_functions(instruction)

        # Write out the files.
        data_source_base_name = os.path.basename(SPIRV_GRAMMAR_FILE)
        builder_template_args = {
            'script_name': os.path.basename(sys.argv[0]),
            'data_source_name': data_source_base_name,
            'file_name': SPIRV_BUILDER_FILE,
            'file_name_capitalized': remove_chars(SPIRV_BUILDER_FILE.upper(), '_'),
            'verb': 'generate',
            'helper_functions': BUILDER_HELPER_FUNCTIONS,
            'prototype_list': ''.join(self.instruction_builder_prototypes),
            'function_list': ''.join(self.instruction_builder_impl)
        }
        parser_template_args = {
            'script_name': os.path.basename(sys.argv[0]),
            'data_source_name': data_source_base_name,
            'file_name': SPIRV_PARSER_FILE,
            'file_name_capitalized': remove_chars(SPIRV_PARSER_FILE.upper(), '_'),
            'verb': 'parse',
            'helper_functions': '',
            'prototype_list': ''.join(self.instruction_parser_prototypes),
            'function_list': ''.join(self.instruction_parser_impl)
        }

        with (open(self.path_prefix + SPIRV_BUILDER_FILE + '_autogen.h', 'w')) as f:
            f.write(HEADER_TEMPLATE.format(**builder_template_args))

        with (open(self.path_prefix + SPIRV_BUILDER_FILE + '_autogen.cpp', 'w')) as f:
            f.write(SOURCE_TEMPLATE.format(**builder_template_args))

        with (open(self.path_prefix + SPIRV_PARSER_FILE + '_autogen.h', 'w')) as f:
            f.write(HEADER_TEMPLATE.format(**parser_template_args))

        with (open(self.path_prefix + SPIRV_PARSER_FILE + '_autogen.cpp', 'w')) as f:
            f.write(SOURCE_TEMPLATE.format(**parser_template_args))

    def requires_unsupported_capability(self, item):
        depends = item.get('capabilities', [])
        if len(depends) == 0:
            return False
        return all([dep in self.unsupported_capabilities for dep in depends])

    def requires_unsupported_extension(self, item):
        extensions = item.get('extensions', [])
        return any([ext not in self.supported_extensions for ext in extensions])

    def accumulate_unsupported_capabilities(self):
        operand_kinds = self.grammar['operand_kinds']

        # Find the Capability enum
        for kind in filter(lambda entry: entry['kind'] == 'Capability', operand_kinds):
            capabilities = kind['enumerants']
            for capability in capabilities:
                name = capability['enumerant']
                # For each capability, see if any of the capabilities they depend on is unsupported.
                # If so, add the capability to the list of unsupported ones.
                if self.requires_unsupported_capability(capability):
                    self.unsupported_capabilities.add(name)
                    continue
                # Do the same for extensions
                if self.requires_unsupported_extension(capability):
                    self.unsupported_capabilities.add(name)

    def find_bit_mask_types(self):
        operand_kinds = self.grammar['operand_kinds']

        # Find the BitEnum categories
        for bitEnumEntry in filter(lambda entry: entry['category'] == 'BitEnum', operand_kinds):
            self.bit_mask_types.add(bitEnumEntry['kind'])

    def get_operand_name(self, operand):
        kind = operand['kind']
        name = operand.get('name')

        # If no name is given, derive the name from the kind
        if name is None:
            assert (kind.find(' ') == -1)
            return make_camel_case(kind)

        quantifier = operand.get('quantifier', '')
        name = remove_chars(name, "'")

        # First, a number of special-cases for optional lists
        if quantifier == '*':
            suffix = 'List'

            # For IdRefs, change 'Xyz 1', +\n'Xyz 2', +\n...' to xyzList
            if kind == 'IdRef':
                if name.find(' ') != -1:
                    name = name[0:name.find(' ')]

            # Otherwise, if it's a pair in the form of 'Xyz, Abc, ...', change it to xyzAbcPairList
            elif kind.startswith('Pair'):
                suffix = 'PairList'

            # Otherwise, it's just a list, so change `xyz abc` to `xyzAbcList

            name = remove_chars(name, " ,.")
            return make_camel_case(name) + suffix

        # Otherwise, remove invalid characters and make the first letter lower case.
        name = remove_chars(name, " .,+\n~")

        name = make_camel_case(name)

        # Make sure the name is not a C++ keyword
        return 'default_' if name == 'default' else name

    def get_operand_namespace(self, kind):
        return '' if kind in ANGLE_DEFINED_TYPES else 'spv::'

    def get_operand_type_suffix(self, kind):
        return 'Mask' if kind in self.bit_mask_types else ''

    def get_kind_cpp_type(self, kind):
        return self.get_operand_namespace(kind) + kind + self.get_operand_type_suffix(kind)

    def get_operand_type_in_and_out(self, operand):
        kind = operand['kind']
        quantifier = operand.get('quantifier', '')

        is_array = quantifier == '*'
        is_optional = quantifier == '?'
        cpp_type = self.get_kind_cpp_type(kind)

        if is_array:
            type_in = 'const ' + cpp_type + 'List &'
            type_out = cpp_type + 'List *'
        elif is_optional:
            type_in = 'const ' + cpp_type + ' *'
            type_out = cpp_type + ' *'
        else:
            type_in = cpp_type
            type_out = cpp_type + ' *'

        return (type_in, type_out, is_array, is_optional)

    def get_operand_push_back_line(self, operand, operand_name, is_array, is_optional):
        kind = operand['kind']
        pre = ''
        post = ''
        accessor = '.'
        item = operand_name
        item_dereferenced = item
        if is_optional:
            # If optional, surround with an if.
            pre = 'if (' + operand_name + ') {\n'
            post = '\n}'
            accessor = '->'
            item_dereferenced = '*' + item
        elif is_array:
            # If array, surround with a loop.
            pre = 'for (const auto &operand : ' + operand_name + ') {\n'
            post = '\n}'
            item = 'operand'
            item_dereferenced = item

        # Usually the operand is one uint32_t, but it may be pair.  Handle the pairs especially.
        if kind == 'PairLiteralIntegerIdRef':
            line = 'blob->push_back(' + item + accessor + 'literal);'
            line += 'blob->push_back(' + item + accessor + 'id);'
        elif kind == 'PairIdRefLiteralInteger':
            line = 'blob->push_back(' + item + accessor + 'id);'
            line += 'blob->push_back(' + item + accessor + 'literal);'
        elif kind == 'PairIdRefIdRef':
            line = 'blob->push_back(' + item + accessor + 'id1);'
            line += 'blob->push_back(' + item + accessor + 'id2);'
        elif kind == 'LiteralString':
            line = '{size_t d = blob->size();'
            line += 'blob->resize(d + strlen(' + item_dereferenced + ') / 4 + 1, 0);'
            # We currently don't have any big-endian devices in the list of supported platforms.
            # Literal strings in SPIR-V are stored little-endian (SPIR-V 1.0 Section 2.2.1, Literal
            # String), so if a big-endian device is to be supported, the string copy here should
            # be adjusted.
            line += 'ASSERT(IsLittleEndian());'
            line += 'strcpy(reinterpret_cast<char *>(blob->data() + d), ' + item_dereferenced + ');}'
        else:
            line = 'blob->push_back(' + item_dereferenced + ');'

        return pre + line + post

    def get_operand_parse_line(self, operand, operand_name, is_array, is_optional):
        kind = operand['kind']
        pre = ''
        post = ''
        accessor = '->'

        if is_optional:
            # If optional, surround with an if, both checking if argument is provided, and whether
            # it exists.
            pre = 'if (' + operand_name + ' && _o < _length) {\n'
            post = '\n}'
        elif is_array:
            # If array, surround with an if and a loop.
            pre = 'if (' + operand_name + ') {\n'
            pre += 'while (_o < _length) {\n'
            post = '\n}}'
            accessor = '.'

        # Usually the operand is one uint32_t, but it may be pair.  Handle the pairs especially.
        if kind == 'PairLiteralIntegerIdRef':
            if is_array:
                line = operand_name + '->emplace_back(' + kind + '{LiteralInteger(_instruction[_o]), IdRef(_instruction[_o + 1])});'
                line += '_o += 2;'
            else:
                line = operand_name + '->literal = LiteralInteger(_instruction[_o++]);'
                line += operand_name + '->id = IdRef(_instruction[_o++]);'
        elif kind == 'PairIdRefLiteralInteger':
            if is_array:
                line = operand_name + '->emplace_back(' + kind + '{IdRef(_instruction[_o]), LiteralInteger(_instruction[_o + 1])});'
                line += '_o += 2;'
            else:
                line = operand_name + '->id = IdRef(_instruction[_o++]);'
                line += operand_name + '->literal = LiteralInteger(_instruction[_o++]);'
        elif kind == 'PairIdRefIdRef':
            if is_array:
                line = operand_name + '->emplace_back(' + kind + '{IdRef(_instruction[_o]), IdRef(_instruction[_o + 1])});'
                line += '_o += 2;'
            else:
                line = operand_name + '->id1 = IdRef(_instruction[_o++]);'
                line += operand_name + '->id2 = IdRef(_instruction[_o++]);'
        elif kind == 'LiteralString':
            # The length of string in words is ceil((strlen(str) + 1) / 4).  This is equal to
            # (strlen(str)+1+3) / 4, which is equal to strlen(str)/4+1.
            assert (not is_array)
            # See handling of LiteralString in get_operand_push_back_line.
            line = 'ASSERT(IsLittleEndian());'
            line += '*' + operand_name + ' = reinterpret_cast<const char *>(&_instruction[_o]);'
            line += '_o += strlen(*' + operand_name + ') / 4 + 1;'
        else:
            if is_array:
                line = operand_name + '->emplace_back(_instruction[_o++]);'
            else:
                line = '*' + operand_name + ' = ' + self.get_kind_cpp_type(
                    kind) + '(_instruction[_o++]);'

        return pre + line + post

    def process_operand(self, operand, cpp_operands_in, cpp_operands_out, cpp_in_parse_lines,
                        cpp_out_push_back_lines):
        operand_name = self.get_operand_name(operand)
        type_in, type_out, is_array, is_optional = self.get_operand_type_in_and_out(operand)

        # Make the parameter list
        cpp_operands_in.append(', ' + type_in + ' ' + operand_name)
        cpp_operands_out.append(', ' + type_out + ' ' + operand_name)

        # Make the builder body lines
        cpp_out_push_back_lines.append(
            self.get_operand_push_back_line(operand, operand_name, is_array, is_optional))

        # Make the parser body lines
        cpp_in_parse_lines.append(
            self.get_operand_parse_line(operand, operand_name, is_array, is_optional))

    def generate_instruction_functions(self, instruction):
        name = instruction['opname']
        assert (name.startswith('Op'))
        name = name[2:]

        # Skip if the instruction depends on a capability or extension we aren't interested in
        if self.requires_unsupported_capability(instruction):
            return
        if self.requires_unsupported_extension(instruction):
            return

        operands = instruction.get('operands', [])

        # Skip if any of the operands are not supported
        if any([operand['kind'] in self.unsupported_kinds for operand in operands]):
            return

        cpp_operands_in = []
        cpp_operands_out = []
        cpp_in_parse_lines = []
        cpp_out_push_back_lines = []

        for operand in operands:
            self.process_operand(operand, cpp_operands_in, cpp_operands_out, cpp_in_parse_lines,
                                 cpp_out_push_back_lines)

            # get_operand_parse_line relies on there only being one array parameter, and it being
            # the last.
            assert (operand.get('quantifier') != '*' or len(cpp_in_parse_lines) == len(operands))

            if operand['kind'] == 'Decoration':
                # Special handling of Op*Decorate instructions with a Decoration operand.  That
                # operand always comes last, and implies a number of LiteralIntegers to follow.
                assert (len(cpp_in_parse_lines) == len(operands))

                decoration_operands = {
                    'name': 'values',
                    'kind': 'LiteralInteger',
                    'quantifier': '*'
                }
                self.process_operand(decoration_operands, cpp_operands_in, cpp_operands_out,
                                     cpp_in_parse_lines, cpp_out_push_back_lines)

            elif operand['kind'] == 'ExecutionMode':
                # Special handling of OpExecutionMode instruction with an ExecutionMode operand.
                # That operand always comes last, and implies a number of LiteralIntegers to follow.
                assert (len(cpp_in_parse_lines) == len(operands))

                execution_mode_operands = {
                    'name': 'operands',
                    'kind': 'LiteralInteger',
                    'quantifier': '*'
                }
                self.process_operand(execution_mode_operands, cpp_operands_in, cpp_operands_out,
                                     cpp_in_parse_lines, cpp_out_push_back_lines)

            elif operand['kind'] == 'ImageOperands':
                # Special handling of OpImage* instructions with an ImageOperands operand.  That
                # operand always comes last, and implies a number of IdRefs to follow with different
                # meanings based on the bits set in said operand.
                assert (len(cpp_in_parse_lines) == len(operands))

                image_operands = {'name': 'imageOperandIds', 'kind': 'IdRef', 'quantifier': '*'}
                self.process_operand(image_operands, cpp_operands_in, cpp_operands_out,
                                     cpp_in_parse_lines, cpp_out_push_back_lines)

        # Make the builder prototype body
        builder_prototype = TEMPLATE_BUILDER_FUNCTION_PROTOTYPE.format(
            op=name, param_list=''.join(cpp_operands_in))
        self.instruction_builder_prototypes.append(builder_prototype + ';\n')
        self.instruction_builder_impl.append(
            builder_prototype + '\n' + TEMPLATE_BUILDER_FUNCTION_BODY.format(
                op=name, push_back_lines='\n'.join(cpp_out_push_back_lines)))

        if len(operands) == 0:
            return

        parser_prototype = TEMPLATE_PARSER_FUNCTION_PROTOTYPE.format(
            op=name, param_list=''.join(cpp_operands_out))
        self.instruction_parser_prototypes.append(parser_prototype + ';\n')
        self.instruction_parser_impl.append(
            parser_prototype + '\n' + TEMPLATE_PARSER_FUNCTION_BODY.format(
                op=name, parse_lines='\n'.join(cpp_in_parse_lines)))


def main():

    # auto_script parameters.
    if len(sys.argv) > 1:
        if sys.argv[1] == 'inputs':
            print(SPIRV_GRAMMAR_FILE)
        elif sys.argv[1] == 'outputs':
            output_files_base = [SPIRV_BUILDER_FILE, SPIRV_PARSER_FILE]
            output_files = [
                '_autogen.'.join(pair)
                for pair in itertools.product(output_files_base, ['h', 'cpp'])
            ]
            print(','.join(output_files))
        else:
            print('Invalid script parameters')
            return 1
        return 0

    writer = Writer()
    writer.write_builder_and_parser()

    return 0


if __name__ == '__main__':
    sys.exit(main())
