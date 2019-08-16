import io
import logging
import struct
import pefile
from binascii import hexlify
from .helpers import *
from .structures import *
from .objects import *
from .utils import unpack_stream


class PEHandler(object):
    """
    The main parser, which takes a filename or pefile object and extracts
    information.  If pythia is updated to parse other file types then
    much of the code will need splitting out into a separate class.
    """

    # TODO: Support callbacks, which will allow other programs (idapython,
    #       radare) to use this programatically.  Ideally these should be
    #       passed to the higher level class.

    # TODO: Support parsing a single section, if given the data and the
    #       base virtual address.  This will permit usage from within
    #       IDA.

    _pe = None
    profiles = {
        "delphi_legacy": {
            "description": "Delphi (legacy)",
            "distance": 0x4C,
        },
        "delphi_modern": {
            "description": "Delphi (modern)",
            "distance": 0x58,
        },
    }
    chosen_profile = None
    visited = None  # TODO: Is this required?
    candidates = None  # TODO: Is this required?

    def __init__(self, logger, context, filename=None, pe=None):
        # TODO: Create our own logger
        self.logger = logger
        self.context = context
        self._reset_queues(reset_visited=True)

        if filename:
            self._from_file(filename)

        elif pe:
            self._from_pefile(pe)

    def _reset_queues(self, reset_visited=False):
        """
        Initialise (or reset) the local work queues.  By default the queue
        of visited locations is not reset, as this should only occur once at
        startup.
        """

        # TODO: Scrap me in favour of a work queue object

        if reset_visited:
            self.visited = set()

        self.candidates = {}
        self.found = []

    def _from_pefile(self, pe):
        """
        Initialise with an existing pefile object, useful when some other
        script has already created the object.
        """
        self._pe = pe
        self._pehelper = PEHelper(pe)
        self._mapped_data = self._pe.get_memory_mapped_image()

        # TODO: Validate 32bit.  Need to find 64bit samples to add parsing.
        self._extract_access_license(pe)
        self._extract_packageinfo(pe)

        self.logger.debug(
            "ImageBase is: 0x{:08x}".format(self._pe.OPTIONAL_HEADER.ImageBase)
        )

    def _from_file(self, filename):
        """
        Initialise from a file on disk.
        """

        # TODO: Exception handling - test with junk data
        pe = pefile.PE(filename, fast_load=True)
        self.logger.info("Loading PE from file {}".format(filename))
        self._from_pefile(pe)

    def _extract_access_license(self, pe):
        """
        Extract information from the DVCLAL resource.
        """

        helper = LicenseHelper()
        resource_type = pefile.RESOURCE_TYPE["RT_RCDATA"]
        data = self._pehelper.get_resource_data(resource_type, "DVCLAL")

        if data:
            license = helper.from_bytes(data)
            if license:
                self.logger.info(
                    "Found Delphi %s license information in PE resources", license
                )
                self.context.license = license
            else:
                self.logger.warning(
                    "Unknown Delphi license %s", hexlify(license)
                )

        else:
            self.logger.warning(
                "Did not find DVCLAL license information in PE resources"
            )

    def _extract_packageinfo(self, pe):
        """
        Extract information about what units this executable contains.
        """

        helper = PackageInfoHelper()
        resource_type = pefile.RESOURCE_TYPE["RT_RCDATA"]
        data = self._pehelper.get_resource_data(resource_type, "PACKAGEINFO")

        if data:
            # TODO: Get the output and do something with it
            helper.from_bytes(data)
            self.context.units = helper

        else:
            self.logger.warning(
                "Did not find PACKAGEINFO DVCLAL license information in PE resources"
            )

    def analyse(self):

        # TODO: Find a sample that has objects in more than one section,
        #       as this will break a number of assumptions made throughout

        # TODO: This is incompatible with an API for IDA / Ghidra that takes data from
        #       one section.

        self.work_queue = WorkQueue()

        units = UnitInitHelper(self._pehelper)
        table_pos = units.find_init_table()
        if table_pos:
            self.logger.debug("Unit initialisation table is at 0x{:08x}".format(table_pos))
        else:
            # TODO: Implement a brute force mechanism as an alternative.
            self.logger.error("Unit initialisation table not found, cannot continue")
            return

        # There may be multiple code sections.  Find the one containing the unit initialisation
        # table and process it.  Multiple sections containing Delphi objects are not currently
        # supported, and it is unknown whether this would be generated by the Delphi compiler.
        sections = self._find_code_sections()
        section_names = ", ".join(s.name for s in sections)
        self.logger.debug("Found {} code section(s) named: {}".format(len(sections), section_names))
        code_section = None

        self.context.code_sections = sections
        self.context.data_sections = self._find_data_sections()
        self.logger.debug(self.context)

        for s in sections:
            if s.contains_va(s.load_address):
                self.logger.debug("Unit initialisation table is in section {}".format(s.name))
                code_section = s
                break

        if code_section is None:
            self.logger.error("Could not find code section containing the entry point (whilst looking for unit initialisation table), cannot continue")
            return

        self.logger.info("Analysing section {}".format(s.name))
        init_table = units.parse_init_table(code_section, table_pos, self.context)

        # Get crude Delphi version (<2010 or >=2010), which allows targeting of vftable search
        # strategy.  We check if the Unit Initialisation Table has a member named NumTypes,
        # which is the first of four extra fields introduced by Delphi 2010.
        try:
            num_types = init_table.fields["NumTypes"]
            self.logger.info("Executable seems to be generated by Delphi 2010+")
            modern_delphi = True
        except KeyError:
            modern_delphi = False
            self.logger.info("Executable seems to be generated by an earlier version of Delphi (pre 2010)")

        # Additional version detection strategies:
        #  - Look for "extra data" marker from newer Delphi, followed by alignment padding
        #    (e.g. 02 00 8b c0 is "2 bytes extra data" followed by alignment)
        #  - Size of various vftables / objects
        #  - "Embarcadero Delphi for Win" string in newer versions
        #  - "string" vs "String" (from DIE)

        found = False

        for s in sections:

            # Step 1 - hunt for vftables
            vftables = self._find_vftables(s, modern_delphi)
            self.logger.debug(vftables)

            if vftables:
                if not found:
                    found = True
                else:
                    self.logger.warning(
                        "Have already found potential objects in a different section"
                    )
                    # FIXME: Find an example file to trigger this & test improvements.
                    #        Github issue #3.
                    raise Exception("Objects in more than one section")

            self.logger.info("Finished analysing section {}".format(s.name))

        item = self.work_queue.get_item()
        while item:

            try:
                obj = item["item_type"](code_section, item["location"], work_queue=self.work_queue)
                self.context.items.append(obj)
                self.logger.debug(obj)

            except ValidationError:
                # This is fine for Vftables found during the manual scan (as there may be
                # false positives) but should not normally happen otherwise.
                self.logger.debug("Could not validate object type {} at {:08x}".format(item["item_type"], item["location"]))

            item = self.work_queue.get_item()

        self.logger.debug(self.work_queue._queue)
        # TODO: Ensure the top class is always TObject, or warn
        # TODO: In strict mode, ensure no found items overlap (offset + length)
        # TODO: Check all parent classes have been found during the automated scan
        # TODO: Build up a hierarchy of classes

    def _find_sections(self, flags=None):
        sections = []

        # Check each code segment to see if it has the code flag
        for section in self._pe.sections:
            if flags and section.Characteristics & flags:
                sections.append(PESection(section, self._mapped_data))

        return sections

    def _find_code_sections(self):
        """
        Obtain a list of PESection objects for code sections.
        """
        return self._find_sections(pefile.SECTION_CHARACTERISTICS["IMAGE_SCN_CNT_CODE"])

    def _find_data_sections(self):
        """
        Obtain a list of PESection objects for code sections.
        """
        return self._find_sections(pefile.SECTION_CHARACTERISTICS["IMAGE_SCN_CNT_INITIALIZED_DATA"])

    def _find_vftables(self, section, modern_delphi):
        """
        """
        i = 0
        found = 0

        # Crude distinction between Delphi versions.
        # TODO: This needs further work to check whether other Delphi versions potentially
        #       have different distances.
        if modern_delphi:
            distance = 0x58
        else:
            distance = 0x4c

            # Delphi 3
            #distance = 0x40

        while i < section.size - distance:
            section.data.seek(i)
            (ptr, ) = unpack_stream("I", section.data)

            # Calculate the virtual address of this location
            va = section.load_address + i
            # TODO: Enable when better logging granularity is available
            #self.logger.debug("i is {} and VA 0x{:08x} points to 0x{:08x}".format(i, va, ptr))

            if (va + distance) == ptr:

                # Validate the first five DWORDs.  Regardless of Delphi version these
                # should be 0 (not set) or a pointer within this section.  This helps to
                # reduce the number of false positives we add to the work queue.
                #
                # A more thorough check is conducted when parsing this into an object later,
                # but this simple test useful.
                j = 5
                error = False

                while j:
                    (ptr, ) = unpack_stream("I", section.data)
                    if ptr != 0 and not section.contains_va(ptr):
                        error = True
                    j -= 1

                if not error:
                    found += 1
                    self.logger.debug("Found a potential vftable at 0x{:08x}".format(va))
                    self.work_queue.add_item(va, Vftable)

            # FIXME: 32-bit assumption, see Github issue #6
            i += 4

        # TODO: If we don't find any matches, scan the section manually
        #       for any presence of \x07TOBJECT.  Github issue #4.
        return found
