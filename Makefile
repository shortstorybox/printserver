PORT := 2888
VERSION = $(shell grep '^version *= *' pyproject.toml|sed 's/^version *= *\"//;s/\".*//g')
NEXT_VERSION = $(shell git-cliff --no-exec --bumped-version 2>/dev/null|sed 's/^v//')
PY_FILES := $(shell find printserver -name '*.py')
CERT_TEAM_ID := PD7WK7PS94
CERT_TEAM_NAME := Short Story, Inc.
CERT_APPLICATION := Developer ID Application: $(CERT_TEAM_NAME) ($(CERT_TEAM_ID))
CERT_INSTALLER := Developer ID Installer: $(CERT_TEAM_NAME) ($(CERT_TEAM_ID))
COMPANY_DOMAIN := shortstorybox.com
.DELETE_ON_ERROR: # Delete targets if any command fails

.PHONY: all
all: dist/PrintServer.pkg

.setup:
	@HOMEBREW_NO_AUTO_UPDATE=1 brew install --quiet uv gh git-cliff poppler pyright
	@touch .setup

.PHONY: run
run: .setup
	@# To use a custom port, run `make run PORT=1234`
	@uv run printserver --port $(PORT)

.PHONY: lint
lint: .setup
	@uv lock --check && \
	 uv run ruff check --quiet --no-fix printserver/ && \
	 uv run ruff format --quiet --check printserver/ || ( \
	     echo '\n❌ Formatting issues found. Consider running `make fix`.' >&2; exit 1)
	@uv run pyright --warnings --level warning printserver/
	@!(grep -RIni --color=auto '[X]XX\|[D]O NOT MERGE\|[D]O NOT SUBMIT' printserver/ && \
	   echo '\n❌ Found unresolved comments.')

.PHONY: fix
fix: .setup
	uv lock
	uv run ruff check --quiet --fix printserver/
	uv run ruff format --quiet printserver/
	uv run pyright --warnings --level warning printserver/
	@!(grep -RIni --color=auto '[X]XX\|[D]O NOT MERGE\|[D]O NOT SUBMIT' printserver/ && \
	   echo '\n❌ Found unresolved comments.')

.PHONY: clean
clean:
	rm -rf dist build .venv .setup

.INTERMEDIATE: dist/__main__
dist/__main__: .python-version pyproject.toml uv.lock .setup $(PY_FILES) | lint signing-keys
	@rm -rf build/__main__/ dist/__main__ # Clean up previous build which can interfere with pyinstaller
	uv run pyinstaller --codesign-identity "$(CERT_TEAM_ID)" --osx-bundle-identifier com.shortstorybox.PrintServer \
		--specpath build --onefile --target-arch arm64 printserver/__main__.py
	@rm -rf build/__main__.spec build/__main__/

.INTERMEDIATE: build/package.pkg
build/package.pkg: dist/__main__ pyproject.toml macOS/com.shortstorybox.PrintServer.plist macOS/scripts/*
	@rm -rf build/package/
	mkdir -p build/package/usr/local/bin/ build/package/Library/LaunchDaemons/
	cp macOS/com.shortstorybox.PrintServer.plist build/package/Library/LaunchDaemons/
	cp dist/__main__ build/package/usr/local/bin/printserver
	pkgbuild --root build/package --identifier com.shortstorybox.PrintServer \
		--version "$(VERSION)" --install-location / --ownership recommended \
		--scripts macOS/scripts build/package.pkg
	@rm -rf build/package/

.INTERMEDIATE: build/signed.pkg
build/signed.pkg: build/package.pkg macOS/distribution.xml | signing-keys
	@rm -rf build/signed.pkg
	@echo Running productbuild...
	@productbuild build/signed.pkg --distribution macOS/distribution.xml --package-path build/ --sign '$(CERT_INSTALLER)'

dist/PrintServer.pkg: build/signed.pkg | signing-keys
	@cp -f build/signed.pkg dist/PrintServer.pkg
	@echo Running notarytool...
	@xcrun notarytool history --keychain-profile 'notary-profile' &>/dev/null || (\
	   echo '\n❌ Notarization Profile not found. To create it:\n' \
	      '    1. Create an App-Specific Password: https://account.apple.com/account/manage' \
	      "    2. Store your notary-profile by running: xcrun notarytool store-credentials 'notary-profile' --apple-id='$$(whoami)@$(COMPANY_DOMAIN)' --team-id=$(CERT_TEAM_ID)\n" >&2; \
	   exit 1)
	@xcrun notarytool submit dist/PrintServer.pkg --keychain-profile 'notary-profile' --wait
	xcrun stapler staple dist/PrintServer.pkg

.PHONY: version-bump
version-bump: | lint warn-uncommitted-diffs
	git fetch
	@[[ "$(VERSION)" != "$(NEXT_VERSION)" ]] || (\
	    echo '❌ Version in pyproject.toml ($(VERSION)) already matches the bumped git tag ($(NEXT_VERSION)).' >&2; exit 1)
	@sed -i.bak 's/version *=.*/version = "$(NEXT_VERSION)"  # Updated by `make version-bump`/' pyproject.toml
	@rm pyproject.toml.bak
	uv sync # Update version in the uv.lock file

.PHONY: release
release: | lint warn-uncommitted-diffs
	git fetch
	@[[ "$(NEXT_VERSION)" = "$(VERSION)" ]] || (\
	    echo '❌ Version in local pyproject.toml ($(VERSION)) does not match the bumped git tag ($(NEXT_VERSION)).' >&2; exit 1)
	@[[ "$(NEXT_VERSION)" = "$$(git grep -h '^version *= *"' origin/main -- pyproject.toml|sed 's/.*"\(.*\)".*/\1/')" ]] || (\
	    echo '❌ Version in pyproject.toml on origin/main does not match the bumped git tag ($(NEXT_VERSION)).' >&2; exit 1)
	$(MAKE) clean
	$(MAKE) dist/PrintServer.pkg
	mv dist/PrintServer.pkg dist/PrintServer-v$(VERSION).pkg
	@GH_PROMPT_DISABLED= gh release create v"$(VERSION)" \
	    --generate-notes --title="Release v$(VERSION)" dist/PrintServer-v$(VERSION).pkg

.PHONY: warn-uncommitted-diffs
warn-uncommitted-diffs:
	@git diff --quiet && git diff --quiet --cached || (\
	    read -p "Warning: Your git repository has uncommitted changes. Continue? [y/N] " -r &&\
	    [ y = "$$REPLY" -o Y = "$$REPLY" ] || exit 1)

.PHONY: signing-keys
signing-keys:
	@security find-identity -v -p codesigning|grep -q '$(CERT_APPLICATION)' && \
	 security find-identity -v -p basic|grep -q '$(CERT_INSTALLER)' || (\
	   echo '\n❌ Signing keys not found. To add them:\n' \
	      '    1. Download intermediate Apple certificates: https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer\n' \
	      '    2. Download our private keys: https://app.teampassword.com/dashboard#account/753842\n' \
	      '    3. Double-click the files and add them to the "login" keychain\n' >&2; \
	   exit 1)

