# Git — ежедневные команды
<!-- tags: git, vcs, workflow, history -->

Состояние, ветки, история и безопасная отмена изменений.

## Быстрая проверка

```sh
git status --short --branch
git diff
git diff --staged
git log --oneline --decorate --graph -12
```

`git diff` показывает незастейдженные изменения, а `git diff --staged` — уже подготовленные к коммиту.

## Ветки и синхронизация

```sh
git switch -c feature/name
git fetch --prune
git rebase origin/main
git push --set-upstream origin feature/name
```

| Задача | Команда |
| --- | --- |
| Список веток | `git branch --all` |
| Вернуться назад | `git switch -` |
| Удалить merged-ветку | `git branch -d feature/name` |
| Найти удалённые ветки | `git remote prune origin --dry-run` |

## Подготовить точный коммит

```sh
git add --patch
git diff --staged --check
git commit -m "Describe the change"
```

## Безопасная отмена

```sh
# убрать файл из staged, сохранив изменения
git restore --staged path/to/file

# вернуть один файл из выбранного коммита
git restore --source=HEAD~1 path/to/file

# отменить опубликованный коммит новым коммитом
git revert <commit>
```

> Перед `rebase`, `reset` и удалением веток убедитесь, что нужные изменения сохранены или опубликованы.
