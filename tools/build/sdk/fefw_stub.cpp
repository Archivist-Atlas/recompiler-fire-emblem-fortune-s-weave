// SPDX-License-Identifier: GPL-3.0-or-later
// fefw.exe entry stub built on the player's PC from this file + lifted game libraries + fefw_hle.cpp + fefw_rt.lib.
// Applies NUP Studio pre-launch environment variables (FEFW_RECOMP_NUP, FEFW_FULLSCREEN, FEFW_FPS60, FEFW_VSYNC, FEFW_CONSOLE)
// before handing off to fefw_rt_main() in fefw_rt.dll.
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <fstream>
#include <string>
#include <vector>

#include "fefw_rt_api.h"

#ifndef FEFW_CPU_NEEDS
#define FEFW_CPU_NEEDS FEFW_CPU_NEEDS_FMA_BUILD
#endif

extern "C" {
extern const struct NxModule nxmod_main, nxmod_sdk, nxmod_subsdk0, nxmod_subsdk1, nxmod_subsdk2, nxmod_rtld;
extern const uint32_t nxmod_main_abi, nxmod_sdk_abi, nxmod_subsdk0_abi, nxmod_subsdk1_abi, nxmod_subsdk2_abi, nxmod_rtld_abi;
}

namespace {

std::wstring utf8_to_wide(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    if (n <= 0) return {};
    std::wstring w((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
    return w;
}

std::string wide_to_utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string s((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

bool dir_exists_w(const std::wstring& p) {
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

bool file_exists_w(const std::wstring& p) {
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

std::string env_utf8(const wchar_t* name) {
    DWORD len = GetEnvironmentVariableW(name, nullptr, 0);
    if (len == 0) return {};
    std::wstring w(len, L'\0');
    DWORD got = GetEnvironmentVariableW(name, w.data(), len);
    w.resize(got);
    return wide_to_utf8(w);
}

void set_env_utf8(const wchar_t* name, const std::string& val) {
    std::wstring w = utf8_to_wide(val);
    SetEnvironmentVariableW(name, w.c_str());
    std::string kv = wide_to_utf8(name) + "=" + val;
    _putenv(kv.c_str());
}

void apply_rom_override() {
    std::string nup = env_utf8(L"FEFW_RECOMP_NUP");
    if (nup.empty()) return;
    std::wstring w = utf8_to_wide(nup);
    if (dir_exists_w(w)) {
        // Source folder: check updates/<latest>/romfs, else <dir>/romfs, else <dir> itself
        std::wstring romfs = w + L"\\romfs";
        std::wstring updates = w + L"\\updates";
        if (dir_exists_w(updates)) {
            WIN32_FIND_DATAW fd;
            HANDLE h = FindFirstFileW((updates + L"\\*").c_str(), &fd);
            long long bestVer = -1;
            std::wstring bestRomfs;
            if (h != INVALID_HANDLE_VALUE) {
                do {
                    if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) && fd.cFileName[0] != L'.') {
                        std::wstring cand = updates + L"\\" + fd.cFileName + L"\\romfs";
                        if (dir_exists_w(cand)) {
                            long long v = _wtoi64(fd.cFileName);
                            if (v >= bestVer) {
                                bestVer = v;
                                bestRomfs = cand;
                            }
                        }
                    }
                } while (FindNextFileW(h, &fd));
                FindClose(h);
            }
            if (!bestRomfs.empty()) romfs = bestRomfs;
        }
        if (dir_exists_w(romfs)) {
            set_env_utf8(L"NXRT_FS_ROMFS", wide_to_utf8(romfs));
        } else {
            set_env_utf8(L"NXRT_FS_ROMFS", nup);
        }
    } else if (file_exists_w(w)) {
        set_env_utf8(L"NXRT_FS_ROMFS", nup);
    }
}

void upsert_display_key(std::vector<std::string>& lines, const std::string& key, const std::string& val) {
    bool in_display = false;
    int display_idx = -1;
    int insert_idx = -1;
    for (size_t i = 0; i < lines.size(); ++i) {
        std::string s = lines[i];
        while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
        size_t first = s.find_first_not_of(" \t");
        if (first == std::string::npos) continue;
        if (s[first] == '[') {
            if (in_display) {
                insert_idx = (int)i;
                in_display = false;
            }
            if (s.find("[display]") == first) {
                in_display = true;
                display_idx = (int)i;
            }
            continue;
        }
        if (in_display) {
            size_t eq = s.find('=');
            if (eq != std::string::npos) {
                std::string k = s.substr(first, eq - first);
                while (!k.empty() && (k.back() == ' ' || k.back() == '\t')) k.pop_back();
                if (k == key) {
                    lines[i] = key + " = " + val;
                    return;
                }
            }
        }
    }
    if (display_idx >= 0) {
        if (insert_idx < 0) insert_idx = (int)lines.size();
        lines.insert(lines.begin() + insert_idx, key + " = " + val);
    } else {
        if (!lines.empty() && !lines.back().empty()) lines.push_back("");
        lines.push_back("[display]");
        lines.push_back(key + " = " + val);
    }
}

void apply_display_overrides() {
    std::string fs = env_utf8(L"FEFW_FULLSCREEN");
    std::string fps60 = env_utf8(L"FEFW_FPS60");
    std::string vsync = env_utf8(L"FEFW_VSYNC");
    if (fs.empty() && fps60.empty() && vsync.empty()) return;

    std::vector<std::string> lines;
    {
        std::ifstream in("controls.ini", std::ios::binary);
        if (in.is_open()) {
            std::string line;
            while (std::getline(in, line)) {
                if (!line.empty() && line.back() == '\r') line.pop_back();
                lines.push_back(line);
            }
        } else {
            lines.push_back("[display]");
            lines.push_back("width = 1280");
            lines.push_back("height = 720");
        }
    }

    if (fs == "1") upsert_display_key(lines, "window_mode", "borderless");
    else if (fs == "0") upsert_display_key(lines, "window_mode", "windowed");

    if (fps60 == "1") upsert_display_key(lines, "fps_limit", "60");
    else if (fps60 == "0") upsert_display_key(lines, "fps_limit", "30");

    if (vsync == "1") upsert_display_key(lines, "vsync", "true");
    else if (vsync == "0") upsert_display_key(lines, "vsync", "false");

    std::ofstream out("controls.ini", std::ios::binary | std::ios::trunc);
    if (out.is_open()) {
        for (const auto& l : lines) {
            out << l << "\n";
        }
    }
}

}  // namespace

int main(int argc, char** argv) {
    std::string con = env_utf8(L"FEFW_CONSOLE");
    if (con == "1") {
        if (AllocConsole()) {
            FILE* f = nullptr;
            freopen_s(&f, "CONOUT$", "w", stdout);
            freopen_s(&f, "CONOUT$", "w", stderr);
        }
    }
    apply_rom_override();
    apply_display_overrides();

    static const FefwRtModules mods = {
        sizeof(FefwRtModules), FEFW_RT_API,
        &nxmod_rtld, &nxmod_main, &nxmod_sdk, &nxmod_subsdk0, &nxmod_subsdk1, &nxmod_subsdk2,
        &nxmod_rtld_abi, &nxmod_main_abi, &nxmod_sdk_abi, &nxmod_subsdk0_abi, &nxmod_subsdk1_abi, &nxmod_subsdk2_abi,
        FEFW_CPU_NEEDS,
    };
    return fefw_rt_main(argc, argv, &mods);
}
