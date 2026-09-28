module.exports = {
    extends: ['@commitlint/config-angular'],
    rules: {
        // config-angular allows 72, which Renovate's generated headers exceed for long dependency
        // names, e.g. "chore(deps): update pre-commit hook alessandrojcm/commitlint-pre-commit-hook to v9.27.0".
        'header-max-length': [2, 'always', 100],
        'type-enum': [
            2,
            'always',
            [
                // Default: https://github.com/conventional-changelog/commitlint/blob/master/%40commitlint/config-angular-type-enum/index.js
                'build',
                'ci',
                'docs',
                'feat',
                'fix',
                'perf',
                'refactor',
                'revert',
                'style',
                'test',
                // Added: https://github.com/conventional-changelog/commitlint/#what-is-commitlint
                'chore',
            ],
        ],
    },
};
