import os
import shutil
from plistlib import Plist
from util.util import *
from unixprofile import UnixProfile
from profile import Profile
import stat

class DarwinProfile (UnixProfile):
	def __init__ (self, prefix = None, m64 = False, min_version = 6):
		UnixProfile.__init__ (self, prefix)
		
		self.name = 'darwin'
		self.m64 = m64

		if os.path.exists (self.prefix):
			error ('Prefix %s exists, and may interfere with the staged build. Please remove and try again.' % self.prefix)

		sdkroot = '/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/'
		if (not os.path.isdir (sdkroot)):
			sdkroot = '/Developer/SDKs/'

		sdk_paths = (sdkroot + 'MacOSX10.%s.sdk' % v for v in range (min_version, 20)) #future-proof! :P

		self.mac_sdk_path = None

		for sdk in sdk_paths:
			if os.path.isdir (sdk):
				self.mac_sdk_path = sdk
				break

		if self.mac_sdk_path is None: error ('Mac OS X SDK (>=10.%s) not found under %s' % (min_version, sdkroot))

		self.gcc_flags.extend ([
				'-D_XOPEN_SOURCE',
				'-isysroot %s' % self.mac_sdk_path,
				'-Wl,-headerpad_max_install_names' #needed to ensure install_name_tool can succeed staging binaries
			])

		self.ld_flags.extend ([
				'-headerpad_max_install_names' #needed to ensure install_name_tool can succeed staging binaries
			])

		self.target_osx = '10.%s' % min_version

		if min_version:
			self.gcc_flags.extend (['-mmacosx-version-min=%s' % self.target_osx])
			self.env.set ('MACOSX_DEPLOYMENT_TARGET', self.target_osx)
		
		if self.cmd_options.debug is True:
			self.gcc_flags.extend ([ '-O0', '-ggdb3' ])

		if os.getenv('BOCKBUILD_USE_CCACHE') is None:
			self.env.set ('CC',  'xcrun gcc')
			self.env.set ('CXX', 'xcrun g++')
		else:
			self.env.set ('CC',  'ccache xcrun gcc')
			self.env.set ('CXX', 'ccache xcrun g++')

		if self.arch == 'default':
			self.arch = 'darwin-32'

		# GTK2_RC_FILES must be a ":"-seperated list of files (NOT a single folder)
		self.gtk2_rc_files = os.path.join (os.getcwd (), 'skeleton.darwin', 'Contents', 'Resources', 'etc', 'gtk-2.0', 'gtkrc')
		self.env.set ('GTK2_RC_FILES', '%{gtk2_rc_files}')

	
	def arch_build (self, arch, package):
		if arch == 'darwin-universal':
			package.local_ld_flags = ['-arch i386' , '-arch x86_64']
			package.local_gcc_flags = ['-arch i386' , '-arch x86_64']
		elif arch == 'darwin-32':
			package.local_ld_flags = ['-arch i386','-m32']
			package.local_gcc_flags = ['-arch i386','-m32']
			package.local_configure_flags = ['--build=i386-apple-darwin11.2.0', '--disable-dependency-tracking']
		elif arch == 'darwin-64':
			package.local_ld_flags = ['-arch x86_64 -m64']
			package.local_gcc_flags = ['-arch x86_64 -m64']
			package.local_configure_flags = ['--disable-dependency-tracking']
		else:
			error ('Unknown arch %s' % arch)

		package.local_configure_flags.extend (['--cache-file=%s/%s-%s.cache' % (self.build_root, package.name, arch)])

	def process_package (self, package):
		failure_count = 0
		def staging_harness (path, func, failure_count = failure_count):
			def relocate_to_profile (token):
				if token.find (package.staged_prefix) == -1 and token.find (package.staged_profile) == -1:
					newtoken = token.replace(package.package_prefix, package.staged_profile)
				else:
					newtoken = token.replace(package.staged_prefix, package.staged_profile)

				if newtoken != token:
					package.trace ('%s:\n\t%s\t->\t%s' % (os.path.basename(path), token, newtoken))
				return newtoken

			if (path.endswith ('.release')):
				error ('Staging backup exists in dir we''re trying to stage: %s' % path)

			backup = path + '.release'
			shutil.copy2 (path, backup)
			try:
				func (path, relocate_to_profile)
				if os.path.exists (path + '.stage'):
					package.rm (path)
					shutil.copy2 (path + '.stage', path)
					shutil.copystat (backup, path)
			except CommandException as e:
				package.rm_if_exists (path)
				shutil.copy2 (backup, path)
				package.rm (backup)
				warn ('Staging failed for %s' % os.path.basename (path))
				error (str (e))
				failure_count = failure_count + 1
				if failure_count > 10:
					error ('Possible staging issue, >10 staging failures')

		extra_files = [os.path.join (package.staged_prefix, expand_macros (file, package))
			for file in package.extra_stage_files]

		Profile.postprocess (self, [self.stage_textfiles(staging_harness, extra_files),
				self.stage_binaries(staging_harness),
				self.validate_rpaths(package)], package.staged_prefix)

	def process_release (self):
		unprotect_dir (self.staged_prefix, recursive = True)

		def destaging_harness (backup, func):
			path = backup[0:-len ('.release')]
			trace (path)

			def relocate_for_release (token):
				newtoken = token.replace(self.staged_prefix, self.prefix)

				if newtoken != token:
					trace ('%s:\n\t%s\t->\t%s' % (os.path.basename(path), token, newtoken))

				return newtoken

			try:
				func (path, relocate_for_release)
				if os.path.exists (path + '.stage'):
					os.remove (path)
					shutil.move (path + '.stage', path)
					shutil.copystat (backup, path)
				os.remove (backup)

			except Exception as e:
				warn ('Critical: Destaging failed for ''%s''' % path)
				raise

		Profile.postprocess (self, [self.stage_textfiles(destaging_harness),
			self.stage_binaries(destaging_harness)], self.staged_prefix, lambda l: l.endswith ('.release') )

	class validate_text_staging (Profile.FileProcessor):
		problem_files = []
		def __init__ (self, package):
			self.package = package
			Profile.FileProcessor.__init__ (self)
		def match (self, path, filetype):
			return 'text' in filetype
		def process (self, path):
			with open(path) as text:
				stage_name = os.path.basename (self.package.stage_root)
				for line in text:
					if stage_name in line:
						warn ('String ''%s'' was found in %s' % (stage_name, self.relpath (path)))
						self.problem_files.append (self.relpath(path))
		def end (self):
			if len(self.problem_files) > 0:
				error ('Problematic staging files:\n' + '\n'.join (self.problem_files) )

	class validate_rpaths (Profile.FileProcessor):
		def __init__ (self, package):
			self.package = package
			Profile.FileProcessor.__init__ (self)
		def match (self, path, filetype):
			return 'Mach-O' in filetype and not path.endswith ('.a')
		def process (self, path):
			libs = backtick ('otool -L %s' % path)
			for line in libs:
				#parse 'otool -L'
				if not line.startswith ('\t'):
					continue
				rpath = line.strip ().split(' ')[0]
				# if rpath.startswith (self.package.profile.MONO_ROOT): 
				# 	error ('%s is linking to external distribution %s' % (path, rpath))

	class stage_textfiles (Profile.FileProcessor):
		def match (self, path, filetype):
			return path.endswith ('.pc') or path.endswith ('.la') or filetype.endswith ('text executable')

		def process (self, path, fixup_func):
			with open(path) as text:
				output = open(path + '.stage', 'w')
				for line in text:
					tokens = line.split (" ")
					for idx,token in enumerate(tokens):
						remap = fixup_func (token)
						tokens[idx] = remap

					output.write (" ".join(tokens))
				output.close

	class stage_binaries (Profile.FileProcessor):
		def match (self, path, filetype):
			return filetype.startswith('Mach-O') and not path.endswith ('.a')

		def process (self, path, fixup_func):
			staged_path = fixup_func (path)

			run_shell ('install_name_tool -id %s %s' % (staged_path, path), False)

			libs = backtick ('otool -L %s' % path)
			for line in libs:
				#parse 'otool -L'
				if not line.startswith ('\t'):
					continue
				rpath = line.strip ().split(' ')[0]

				remap = fixup_func (rpath)
				if remap != rpath:
					run_shell ('install_name_tool -change %s %s %s' % (rpath, remap, path), False)

	def bundle (self):
		self.make_app_bundle ()

	def make_app_bundle (self):
		plist_path = os.path.join (self.bundle_skeleton_dir, 'Contents', 'Info.plist')
		app_name = 'Unknown'
		plist = None
		if os.path.exists (plist_path):
			plist = Plist.fromFile (plist_path)
			app_name = plist['CFBundleExecutable']
		else:
			print 'Warning: no Contents/Info.plist in .app skeleton'

		self.bundle_app_dir = os.path.join (self.bundle_output_dir, app_name + '.app')
		self.bundle_contents_dir = os.path.join (self.bundle_app_dir, 'Contents')
		self.bundle_res_dir = os.path.join (self.bundle_contents_dir, 'Resources')
		self.bundle_macos_dir = os.path.join (self.bundle_contents_dir, 'MacOS')

		# Create the .app tree, copying the skeleton
		shutil.rmtree (self.bundle_app_dir, ignore_errors = True)
		shutil.copytree (self.bundle_skeleton_dir, self.bundle_app_dir)
		if not os.path.exists (self.bundle_contents_dir): os.makedirs (self.bundle_contents_dir)
		if not os.path.exists (self.bundle_res_dir): os.makedirs (self.bundle_res_dir)
		if not os.path.exists (self.bundle_macos_dir): os.makedirs (self.bundle_macos_dir)

		# Generate the PkgInfo
		pkginfo_path = os.path.join (self.bundle_contents_dir, 'PkgInfo')
		if not os.path.exists (pkginfo_path) and not plist == None:
			fp = open (pkginfo_path, 'w')
			fp.write (plist['CFBundlePackageType'])
			fp.write (plist['CFBundleSignature'])
			fp.close ()

		# Run solitary against the installation to collect files
		files = ''
		for file in self.bundle_from_build:
			files = files + ' "%s"' % os.path.join (self.prefix, file)

		run_shell ('mono --debug ../../solitary/Solitary.exe '
			'--mono-prefix="%s" --root="%s" --out="%s" %s' % \
			(self.prefix, self.prefix, self.bundle_res_dir, files))
		self.configure_gtk ()
		self.configure_gdk_pixbuf ()

	def configure_gtk (self):
		paths = [
			os.path.join ('etc', 'gtk-2.0', 'gtk.immodules'),
			os.path.join ('etc', 'gtk-2.0', 'im-multipress.conf'),
			os.path.join ('etc', 'pango', 'pango.modules')
		]

		for path in paths:
			bundle_path = os.path.join (self.bundle_res_dir, path) + '.in'
			path = os.path.join (self.prefix, path)

			if not os.path.isfile (path):
				continue

			try:
				os.makedirs (os.path.dirname (bundle_path))
			except:
				pass

			ifp = open (path)
			ofp = open (bundle_path, 'w')
			for line in ifp:
				if line.startswith ('#'):
					continue
				ofp.write (line.replace (self.prefix, '${APP_RESOURCES}'))
			ifp.close ()
			ofp.close ()

			if os.path.basename (path) == 'pango.modules':
				fp = open (os.path.join (os.path.dirname (bundle_path), 'pangorc'), 'w')
				fp.write ('[Pango]\n')
				fp.write ('ModuleFiles=./pango.modules\n')
				fp.close ()

	def configure_gdk_pixbuf (self):
		path = os.path.join (self.bundle_res_dir, 'etc', 'gtk-2.0', 'gdk-pixbuf.loaders.in')

		# HACK solitary relocates some .dylib so that gdk-pixbuf-query-loaders will fail
		# if not run from the build-root/_install/lib/ directory
		os.chdir ('%s/lib/' % self.prefix)

		run_shell ('gdk-pixbuf-query-loaders 2>/dev/null | ' + \
			'sed \'s,%s,\\${APP_RESOURCES},g\' 1> "%s"' % (self.prefix, path))
