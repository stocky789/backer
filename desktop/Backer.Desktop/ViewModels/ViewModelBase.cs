using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

public abstract class ViewModelBase : ObservableObject
{
    public abstract string Title { get; }

    /// <summary>Invoked by Enter while this view is visible. Null means Enter does nothing.</summary>
    public virtual IRelayCommand? PrimaryCommand => null;

    /// <summary>Bumped every time the view is shown; background results carrying an older
    /// generation are stale and must be dropped.</summary>
    public int Generation { get; private set; }

    public bool IsVisible { get; private set; }

    public void Enter()
    {
        Generation++;
        IsVisible = true;
        OnShown();
    }

    public void Exit()
    {
        IsVisible = false;
        OnHidden();
    }

    /// <summary>Called each time the view becomes visible.</summary>
    public virtual void OnShown()
    {
    }

    public virtual void OnHidden()
    {
    }
}
