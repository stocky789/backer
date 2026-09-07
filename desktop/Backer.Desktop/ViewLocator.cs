using System;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Backer.Desktop.ViewModels;

namespace Backer.Desktop;

/// <summary>Maps FooViewModel -> Backer.Desktop.Views.FooView.</summary>
public sealed class ViewLocator : IDataTemplate
{
    public Control Build(object? data)
    {
        if (data is null)
        {
            return new TextBlock { Text = "" };
        }
        var name = data.GetType().FullName!
            .Replace("ViewModels", "Views", StringComparison.Ordinal)
            .Replace("ViewModel", "View", StringComparison.Ordinal);
        var type = Type.GetType(name);
        return type is null
            ? new TextBlock { Text = $"View not found: {name}" }
            : (Control)Activator.CreateInstance(type)!;
    }

    public bool Match(object? data) => data is ViewModelBase;
}
