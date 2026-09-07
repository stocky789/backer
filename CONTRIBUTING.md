# Contributing to Backer

Thanks for your interest in contributing! This project is actively evolving, and contributions are welcome.

## How to contribute

1. **Fork** the repository and create your branch from `main`.
2. **Install dev dependencies**:
   ```bash
   pip install -e ".[dev,all]"
   ```
3. **Run tests and linting** before opening a PR:
   ```bash
   make lint
   make test
   ```
   `make test` also builds and tests the C# desktop client when the .NET 8 SDK
   is installed. If you changed anything under `desktop/`, run it directly:
   ```bash
   dotnet build desktop/Backer.Desktop.sln -c Release && dotnet test desktop/Backer.Desktop.sln
   ```
4. **Open a Pull Request** with a clear description of your changes.

## Development guidelines

- Keep changes small and focused when possible.
- Avoid breaking changes unless discussed in an issue first.
- Add tests where practical.
- Document user-facing changes in the README or release notes.

## Support

If you’re unsure about a change, open an issue to discuss it first.
