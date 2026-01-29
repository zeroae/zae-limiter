# Pull Request Workflow

All changes must go through pull requests. Direct commits to `main` are not allowed.

## Workflow

1. Create a feature branch from main:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feat/your-feature-name
   ```

2. Make changes following project conventions:
   - Follow commit message conventions (see commits.md)
   - Add tests for new functionality
   - Update documentation as needed

3. Push and create a pull request:
   ```bash
   git push origin feat/your-feature-name
   ```

4. Wait for CI checks to pass:
   - **Lint**: Code style and formatting (ruff)
   - **Type Check**: Static type checking (mypy)
   - **Tests**: Unit tests with coverage (pytest on Python 3.11 & 3.12)

5. Address review feedback if needed

6. Once approved and CI passes, the PR will be merged to main

**Important:** Never force-push to main or bypass CI checks.
